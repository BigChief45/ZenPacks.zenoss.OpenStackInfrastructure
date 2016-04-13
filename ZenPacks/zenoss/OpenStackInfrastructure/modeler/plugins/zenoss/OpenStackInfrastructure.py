###########################################################################
#
# This program is part of Zenoss Core, an open source monitoring platform.
# Copyright (C) 2014, Zenoss Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 2 or (at your
# option) any later version as published by the Free Software Foundation.
#
# For complete information please visit: http://www.zenoss.com/oss/
#
###########################################################################

""" Get component information using OpenStack API clients """

import logging
log = logging.getLogger('zen.OpenStackInfrastructure')

import json
import os
import re
import itertools
from urlparse import urlparse
import socket
from collections import defaultdict

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.internet.error import ConnectError, TimeoutError

from Products.ZenUtils.IpUtil import isip, asyncIpLookup
from Products.DataCollector.plugins.CollectorPlugin import PythonPlugin
from Products.DataCollector.plugins.DataMaps import ObjectMap, RelationshipMap
from Products.ZenUtils.Utils import prepId
from Products.ZenUtils.Time import isoToTimestamp, LocalDateTime

from ZenPacks.zenoss.OpenStackInfrastructure.utils import (
    add_local_lib_path,
    zenpack_path,
    get_subnets_from_fixedips,
    get_port_instance,
    getNetSubnetsGws_from_GwInfo,
    get_port_fixedips,
    sleep,
    isValidHostname,
    sanitize_host_or_ip,
)

add_local_lib_path()

from apiclients.txapiclient import APIClient, APIClientError, NotFoundError


class OpenStackInfrastructure(PythonPlugin):
    deviceProperties = PythonPlugin.deviceProperties + (
        'zCommandUsername',
        'zCommandPassword',
        'zOpenStackProjectId',
        'zOpenStackAuthUrl',
        'zOpenStackRegionName',
        'zOpenStackNovaApiHosts',
        'zOpenStackExtraHosts',
    )

    _keystonev2errmsg = """
        Unable to connect to keystone Identity Admin API v2.0 to retrieve tenant
        list.  Tenant names will be unknown (tenants will show with their IDs only)
        until this is corrected, either by opening access to the admin API endpoint
        as listed in the keystone service catalog, or by configuring zOpenStackAuthUrl
        to point to a different, accessible endpoint which supports both the public and
        admin APIs.  (This may be as simple as changing the port in the URL from
        5000 to 35357)  Details: %s
    """

    @inlineCallbacks
    def collect(self, device, unused):

        client = APIClient(
            device.zCommandUsername,
            device.zCommandPassword,
            device.zOpenStackAuthUrl,
            device.zOpenStackProjectId,
            is_admin=True)

        results = {}

        results['tenants'] = []
        try:
            result = yield client.keystone_tenants()
            results['tenants'] = result['tenants']
            log.debug('tenants: %s\n' % str(results['tenants']))

        except (ConnectError, TimeoutError), e:
            log.error(self._keystonev2errmsg, e)
        except APIClientError, e:
            if len(e.args):
                if isinstance(e.args[0], ConnectError) or \
                   isinstance(e.args[0], TimeoutError):
                    log.error(self._keystonev2errmsg, e.args[0])
                else:
                    log.error(self._keystonev2errmsg, e)
            else:
                log.error(self._keystonev2errmsg, e)
        except Exception, e:
            log.error(self._keystonev2errmsg, e)

        results['nova_url'] = yield client.nova_url()

        result = yield client.nova_flavors(is_public=True)
        results['flavors'] = result['flavors']
        result = yield client.nova_flavors(is_public=False)
        results['flavors'].extend(result['flavors'])
        log.debug('flavors: %s\n' % str(results['flavors']))

        result = yield client.nova_images()
        results['images'] = result['images']
        log.debug('images: %s\n' % str(results['images']))

        result = yield client.nova_hypervisors(hypervisor_match='%',
                                               servers=True)
        results['hypervisors'] = result['hypervisors']
        log.debug('hypervisors: %s\n' % str(results['hypervisors']))

        result = yield client.nova_hypervisorsdetailed()
        results['hypervisors_detailed'] = result['hypervisors']
        log.debug('hypervisors_detailed: %s\n' % str(results['hypervisors']))

        # get hypervisor details for each individual hypervisor
        results['hypervisor_details'] = {}
        for hypervisor in results['hypervisors']:
            result = yield client.nova_hypervisors(
                hypervisor_id=hypervisor['id'])
            hypervisor_id = prepId("hypervisor-{0}".format(hypervisor['id']))
            results['hypervisor_details'][hypervisor_id] = result['hypervisor']

        result = yield client.nova_servers()
        results['servers'] = result['servers']
        log.debug('servers: %s\n' % str(results['servers']))

        result = yield client.nova_services()
        results['services'] = result['services']
        log.debug('services: %s\n' % str(results['services']))

        # Neutron
        results['agents'] = []
        try:
            result = yield client.neutron_agents()
            results['agents'] = result['agents']
        except NotFoundError:
            log.error("Unable to model neutron agents because the enabled neutron plugin does not support the 'agent' API extension.")
        except Exception, e:
            log.error('Error modeling neutron agents: %s' % e)

        # ---------------------------------------------------------------------
        # Insert the l3_agents -> (routers, networks, subnets, gateways) data
        # ---------------------------------------------------------------------
        for _agent in results['agents']:
            _agent['l3_agent_routers'] = []
            _routers = set()
            _subnets = set()
            _gateways = set()
            _networks = set()

            if _agent['agent_type'].lower() == 'l3 agent':
                try:
                    router_data = yield \
                        client.api_call('/v2.0/agents/%s/l3-routers'
                                        % str(_agent['id']))
                except Exception, e:
                    log.warning("Unable to determine neutron URL for " +
                                "l3 router agent discovery: %s" % e)
                    continue

                for r in router_data['routers']:
                    _routers.add(r.get('id'))
                    (net, snets, gws) = \
                        getNetSubnetsGws_from_GwInfo(r['external_gateway_info'])
                    if net: _networks.add(net)
                    _subnets = _subnets.union(snets)
                    _gateways = _gateways.union(gws)

                _agent['l3_agent_networks'] = list(_networks)
                _agent['l3_agent_subnets'] = list(_subnets)
                _agent['l3_agent_gateways'] = list(_gateways)  # Not used yet
                _agent['l3_agent_routers'] = list(_routers)

        # ---------------------------------------------------------------------
        # Insert the DHCP agents-subnets info
        # ---------------------------------------------------------------------
        for _agent in results['agents']:
            _agent['dhcp_agent_subnets'] = []
            _subnets = []
            _networks = []

            if _agent['agent_type'].lower() == 'dhcp agent':
                try:
                    dhcp_data = yield \
                        client.api_call('/v2.0/agents/%s/dhcp-networks'
                                                % str(_agent['id']))
                except Exception, e:
                    log.warning("Unable to determine neutron URL for " +
                                "dhcp agent discovery: %s" % e)
                    continue

                for network in dhcp_data['networks']:
                    _networks.append(network.get('id'))
                    for subnet in network['subnets']:
                        _subnets.append(subnet)

                _agent['dhcp_agent_subnets'] = _subnets
                _agent['dhcp_agent_networks'] = _networks

        result = yield client.neutron_networks()
        results['networks'] = result['networks']

        result = yield client.neutron_subnets()
        results['subnets'] = result['subnets']

        result = yield client.neutron_routers()
        results['routers'] = result['routers']

        result = yield client.neutron_ports()
        results['ports'] = result['ports']

        result = yield client.neutron_floatingips()
        results['floatingips'] = result['floatingips']

        # Do some DNS lookups as well.
        hostnames = set([x['host'] for x in results['services']])
        hostnames.update([x['host'] for x in results['agents']])
        hostnames.update(device.zOpenStackExtraHosts)
        hostnames.update(device.zOpenStackNovaApiHosts)
        try:
            hostnames.add(urlparse(results['nova_url']).hostname)
        except Exception, e:
            log.warning("Unable to determine nova URL for nova-api component discovery: %s" % e)

        # not being able to resolve hostname could, in some cases,
        # result in nova-api and/or cinder-api components not being modeled
        # make sure DNS can resolve controller hostname

        results['resolved_hostnames'] = {}
        for hostname in sorted(hostnames):
            if isip(hostname):
                results['resolved_hostnames'][hostname] = hostname
                continue

            for i in range(1, 4):
                try:
                    host_ip = yield asyncIpLookup(hostname)
                    results['resolved_hostnames'][hostname] = host_ip
                    break
                except socket.gaierror, e:
                    if e.errno == -3:
                        # temporary dns issue- try again.
                        log.error("resolve %s: (attempt %d/3): %s" % (hostname, i, e))
                        yield sleep(2)
                        continue
                    else:
                        log.error("resolve %s: %s" % (hostname, e))
                        break
                except Exception, e:
                    log.error("resolve %s: %s" % (hostname, e))
                    break

        # Cinder
        result = yield client.cinder_volumes()
        results['volumes'] = result['volumes']

        result = yield client.cinder_volumetypes()
        results['volumetypes'] = result['volume_types']

        result = yield client.cinder_volumesnapshots()
        results['volsnapshots'] = result['snapshots']

        result = yield client.cinder_services()
        results['cinder_services'] = result['services']

        result = yield client.cinder_pools()
        results['volume_pools'] = result['pools']

        results['quotas'] = {}
        for tenant in results['tenants']:
            try:
                result = yield client.cinder_quotas(tenant=tenant['id'].encode(
                    'ascii', 'ignore'), usage=False)
            except Exception, e:
                log.warn("Unable to obtain quotas for %s. Error message: %s" %
                         (tenant['name'], e.message))
            else:
                results['quotas'][tenant['id']] = result['quota_set']

        returnValue(results)

    def process(self, device, results, unused):
        tenants = []
        quota_tenants = []                   # for use by quota later
        for tenant in results['tenants']:
            if tenant.get('enabled', False) is not True:
                continue
            if not tenant.get('id', None):
                continue

            quota_tenant = dict(name=tenant.get('name', tenant['id']),
                                id=tenant['id'])
            quota_tenants.append(quota_tenant)

            tenants.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Tenant',
                data=dict(
                    id=prepId('tenant-{0}'.format(tenant['id'])),
                    title=tenant.get('name', tenant['id']),
                    description=tenant.get('description', ''),
                    tenantId=tenant['id']
                )))

        region_id = prepId("region-{0}".format(device.zOpenStackRegionName))
        region = ObjectMap(
            modname='ZenPacks.zenoss.OpenStackInfrastructure.Region',
            data=dict(
                id=region_id,
                title=device.zOpenStackRegionName
            ))

        flavors = []
        for flavor in results['flavors']:
            if not flavor.get('id', None):
                continue

            flavors.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Flavor',
                data=dict(
                    id=prepId('flavor-{0}'.format(flavor['id'])),
                    title=flavor.get('name', flavor['id']),  # 256 server
                    flavorId=flavor['id'],  # performance1-1
                    flavorRAM=flavor.get('ram', 0) * 1024 * 1024,
                    flavorDisk=flavor.get('disk', 0) * 1024 * 1024 * 1024,
                    flavorVCPUs=flavor.get('vcpus', 0),
                )))

        images = []
        for image in results['images']:
            if not image.get('id', None):
                continue

            # If we can, change dates like '2014-09-30T19:45:44Z' to '2014/09/30 19:45:44.00'
            try:
                imageCreated = LocalDateTime(isoToTimestamp(image.get('created', '').replace('Z', '')))
            except Exception:
                log.debug("Unable to reformat imageCreated '%s'" % image.get('created', ''))
                imageCreated = image.get('created', '')

            try:
                imageUpdated = LocalDateTime(isoToTimestamp(image.get('updated', '').replace('Z', '')))
            except Exception:
                log.debug("Unable to reformat imageUpdated '%s'" % image.get('updated', ''))
                imageUpdated = image.get('updated', '')

            images.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Image',
                data=dict(
                    id=prepId('image-{0}'.format(image['id'])),
                    title=image.get('name', image['id']),  # Red Hat Enterprise Linux 5.5
                    imageId=image['id'],  # 346eeba5-a122-42f1-94e7-06cb3c53f690
                    imageStatus=image.get('status', ''),  # ACTIVE
                    imageCreated=imageCreated,  # 2014/09/30 19:45:44.000
                    imageUpdated=imageUpdated,  # 2014/09/30 19:45:44.000
                )))

        # instances
        servers = []
        for server in results['servers']:
            if not server.get('id', None):
                continue

            # Backup support is optional. Guard against it not existing.
            backup_schedule_enabled = server.get('backup_schedule',
                                                 {}).get('enabled', False)
            backup_schedule_daily = server.get('backup_schedule',
                                               {}).get('daily', 'DISABLED')
            backup_schedule_weekly = server.get('backup_schedule',
                                                {}).get('weekly', 'DISABLED')

            # Get and classify IP addresses into public and private: (fixed/floating)
            public_ips = set()
            private_ips = set()

            access_ipv4 = server.get('accessIPv4')
            if access_ipv4:
                public_ips.add(access_ipv4)

            access_ipv6 = server.get('accessIPv6')
            if access_ipv6:
                public_ips.add(access_ipv6)

            address_group = server.get('addresses')
            if address_group:
                for network_name, net_addresses in address_group.items():
                    for address in net_addresses:
                        if address.get('OS-EXT-IPS:type') == 'fixed':
                            private_ips.add(address.get('addr'))
                        elif address.get('OS-EXT-IPS:type') == 'floating':
                            public_ips.add(address.get('addr'))
                        else:
                            log.info("Address type not found for %s", address.get('addr'))
                            log.info("Adding %s to private_ips", address.get('addr'))
                            private_ips.add(address.get('addr'))

            # Flavor and Image IDs could be specified two different ways.
            flavor_id = server.get('flavorId', None) or \
                        server.get('flavor', {}).get('id', None)

            tenant_id = server.get('tenant_id', '')

            # Note: volume relations are added in volumes map below
            server_dict = dict(
                            id=prepId('server-{0}'.format(server['id'])),
                            title=server.get('name', server['id']),
                            resourceId=server['id'],
                            serverId=server['id'],  # 847424
                            serverStatus=server.get('status', ''),
                            serverBackupEnabled=backup_schedule_enabled,
                            serverBackupDaily=backup_schedule_daily,
                            serverBackupWeekly=backup_schedule_weekly,
                            publicIps=list(public_ips),
                            privateIps=list(private_ips),
                            set_flavor=prepId('flavor-{0}'.format(flavor_id)),
                            set_tenant=prepId('tenant-{0}'.format(tenant_id)),
                            hostId=server.get('hostId', ''),
                            hostName=server.get('name', '')
                            )

            # Some Instances are created from pre-existing volumes
            # This implies no image exists.
            image_id = None
            if 'imageId' in server:
                image_id = server['imageId']
            elif 'image' in server \
                    and isinstance(server['image'], dict) \
                    and 'id' in server['image']:
                image_id = server['image']['id']

            if image_id:
                server_dict['set_image'] = prepId('image-{0}'.format(image_id))

            servers.append(ObjectMap(
                    modname='ZenPacks.zenoss.OpenStackInfrastructure.Instance',
                    data=server_dict))

        services = []
        zones = {}
        hostmap = {}

        # Find all hosts which have a nova service on them.
        for service in results['services']:
            if not service.get('id', None):
                continue

            title = '{0}@{1} ({2})'.format(service.get('binary', ''),
                                           service.get('host', ''),
                                           service.get('zone', ''))
            service_id = prepId('service-{0}-{1}-{2}'.format(
                service.get('binary', ''), service.get('host', ''), service.get('zone', '')))
            host_id = prepId("host-{0}".format(service.get('host', '')))
            zone_id = prepId("zone-{0}".format(service.get('zone', '')))

            hostmap[host_id] = {
                'hostname': service.get('host', ''),
                'org_id': zone_id
            }

            zones.setdefault(zone_id, ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.AvailabilityZone',
                data=dict(
                    id=zone_id,
                    title=service.get('zone', ''),
                    set_parentOrg=region_id
                )))

            # Currently, nova-api doesn't show in the nova service list.
            # Even if it does show up there in the future, I don't model
            # it as a NovaService, but rather as its own type of software
            # component.   (See below)
            if service.get('binary', '') == 'nova-api':
                continue

            services.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.NovaService',
                data=dict(
                    id=service_id,
                    title=title,
                    binary=service.get('binary', ''),
                    enabled={
                        'enabled': True,
                        'disabled': False
                    }.get(service.get('status', None), False),
                    operStatus={
                        'up': 'UP',
                        'down': 'DOWN'
                    }.get(service.get('state', None), 'UNKNOWN'),
                    set_hostedOn=host_id,
                    set_orgComponent=zone_id
                )))

        # Find all hosts which have a neutron agent on them.
        for agent in results['agents']:
            if not agent.get('id', None):
                continue

            sanitized_hostname = sanitize_host_or_ip(agent['host'])
            if not sanitized_hostname:
                log.debug("Skipping empty hostname %s !", agent['host'])
                continue
            if sanitized_hostname != agent['host']:
                log.debug("Sanitized hostname %s", agent['host'])

            host_id = prepId("host-{0}".format(sanitized_hostname))
            hostmap[host_id] = {
                'hostname': sanitized_hostname,
                'org_id': region_id
            }

        # Find all hosts which have a cinder service on them
        # where cinder services are: cinder-backup, cinder-scheduler, cinder-volume
        for service in results['cinder_services']:
            # well, guest what? volume services do not have 'id' key !

            host_id_end = service.get('host', '').find('@')
            if host_id_end > -1:
                host_id = prepId("host-{0}".format(service.get('host', '')[:host_id_end]))
            else:
                host_id = prepId("host-{0}".format(service.get('host', '')))
            zone_id = prepId("zone-{0}".format(service.get('zone', '')))
            title = '{0}@{1} ({2})'.format(service.get('binary', ''),
                                           service.get('host', ''),
                                           service.get('zone', ''))
            service_id = prepId('service-{0}-{1}-{2}'.format(
                service.get('binary', ''), service.get('host', ''), service.get('zone', '')))

            if host_id not in hostmap:
                hostmap[host_id] = {
                    'hostname': service.get('host', ''),
                    'org_id': zone_id
                }
            services.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.CinderService',
                data=dict(
                    id=service_id,
                    title=title,
                    binary=service.get('binary', ''),
                    enabled={
                        'enabled': True,
                        'disabled': False
                    }.get(service.get('status', None), False),
                    operStatus={
                        'up': 'UP',
                        'down': 'DOWN'
                    }.get(service.get('state', None), 'UNKNOWN'),
                    set_hostedOn=host_id,
                    set_orgComponent=zone_id
                )))

        # add any user-specified hosts which we haven't already found.
        if device.zOpenStackNovaApiHosts or device.zOpenStackExtraHosts:
            log.info("Finding additional hosts")

            if device.zOpenStackNovaApiHosts:
                log.info("  Adding zOpenStackNovaApiHosts=%s" % device.zOpenStackNovaApiHosts)
            if device.zOpenStackExtraHosts:
                log.info("  Adding zOpenStackExtraHosts=%s" % device.zOpenStackExtraHosts)

        for hostname in device.zOpenStackNovaApiHosts + device.zOpenStackExtraHosts:
            host_id = prepId("host-{0}".format(hostname))
            hostmap[host_id] = {
                'hostname': hostname,
                'org_id': region_id
            }

        hosts = []
        for host_id in hostmap:
            data = hostmap[host_id]

            if not isValidHostname(data.get('hostname', host_id)):
                # Jake Lampe once got a host like this:
                # u'host': u'overcloud-controller-1.localdomain:4947de00-0c13-5ee7-902e-fc270d3993b9'
                # and the hostname in hostmap becomes:
                # 'host-overcloud-controller-1.localdomain:4947de00-0c13-5ee7-902e-fc270d3993b9'
                # this caused problem when setting orgComponent for agent
                log.warn("  Invalid hostname found: %s" % data.get('hostname', host_id))
                continue

            hosts.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Host',
                data=dict(
                    id=host_id,
                    title=data.get('hostname', host_id),
                    hostname=data.get('hostname', ''),
                    set_orgComponent=data.get('org_id', '')
                )))

        hypervisor_type = {}
        hypervisor_version = {}
        for hypervisor in results['hypervisors_detailed']:
            if not hypervisor.get('id', None):
                continue

            hypervisor_id = prepId("hypervisor-{0}".format(hypervisor['id']))

            hypervisor_type[hypervisor_id] = hypervisor.get('hypervisor_type', None)
            hypervisor_version[hypervisor_id] = hypervisor.get('hypervisor_version', None)

        # if results['hypervisors_detailed'] did not give us hypervisor type,
        # hypervisor version, try results['hypervisors_details']
        for k, v in hypervisor_type.iteritems():
            if v is None and k in results['hypervisor_details']:
                hypervisor_type[k] = \
                    results['hypervisor_details'][k].get(
                        'hypervisor_type', None)
        for k, v in hypervisor_version.iteritems():
            if v is None and k in results['hypervisor_details']:
                h_ver = results['hypervisor_details'][k].get(
                    'hypervisor_version', None)
                if h_ver:
                    h_ver_list = str(h_ver).split('00')
                    hypervisor_version[k] = '.'.join(h_ver_list)

        hypervisors = []
        server_hypervisor_instance_name = {}
        for hypervisor in results['hypervisors']:
            if not hypervisor.get('id', None):
                continue

            hypervisor_id = prepId("hypervisor-{0}".format(hypervisor['id']))

            # this is how a hypervisor discovers the instances belonging to it
            hypervisor_servers = []
            if 'servers' in hypervisor:
                for server in hypervisor['servers']:
                    server_id = 'server-{0}'.format(server.get('uuid', ''))
                    hypervisor_servers.append(server_id)
                    server_hypervisor_instance_name[server_id] = server.get('name', '')

            hypervisor_dict = dict(
                id=hypervisor_id,
                title='{0}.{1}'.format(hypervisor.get('hypervisor_hostname', ''),
                                       hypervisor['id']),
                hypervisorId=hypervisor['id'],  # 1
                hypervisor_type=hypervisor_type.get(hypervisor_id, ''),
                hypervisor_version=hypervisor_version.get(hypervisor_id, None),
                # hypervisor state: power state, UP/DOWN
                hstate=hypervisor.get('state', 'unknown').upper(),
                # hypervisor status: ENABLED/DISABLED
                hstatus=hypervisor.get('status', 'unknown').upper(),
                # hypervisor ip: internal ip address
                host_ip=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('host_ip', None),
                vcpus=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('vcpus', None),
                vcpus_used=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('vcpus_used', None),
                memory=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('memory_mb', None),
                memory_used=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('memory_mb_used', None),
                memory_free=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('free_ram_mb', None),
                disk=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('local_gb', None),
                disk_used=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('local_gb_used', None),
                disk_free=results['hypervisor_details'].get(hypervisor_id,
                                                {}).get('free_disk_gb', None),
                set_instances=hypervisor_servers,
                set_hostByName=hypervisor.get('hypervisor_hostname', ''),
            )

            if hypervisor_dict['hypervisor_type'] == 'VMware vCenter Server':
                # This hypervisor type does not run on a specific host, so
                # omit the host relationship.
                del hypervisor_dict['set_hostByName']

            hypervisors.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Hypervisor',
                data=hypervisor_dict))

        # add hypervisor instance name to the existing server objectmaps.
        for om in servers:
            if om.id in server_hypervisor_instance_name:
                om.hypervisorInstanceName = server_hypervisor_instance_name[om.id]

        # nova-api support.
        # Place it on the user-specified hosts, or also find it if it's
        # in the nova-service list (which we ignored earlier). It should not
        # be, under icehouse, at least, but just in case this changes..)
        nova_api_hosts = set(device.zOpenStackNovaApiHosts)
        cinder_api_hosts = []
        for service in results['services']:
            if service.get('binary', '') == 'nova-api':
                if service.get('host', '') not in nova_api_hosts:
                    nova_api_hosts.add(service.get('host', ''))
            if service.get('binary', '') == 'cinder-api':
                if service.get('host', '') not in cinder_api_hosts:
                    cinder_api_hosts.append(service.get('host', ''))

        # Look to see if the hostname or IP in the auth url corresponds
        # directly to a host we know about.  If so, add it to the nova
        # api hosts.
        try:
            apiHostname = urlparse(results['nova_url']).hostname
            apiIp = results['resolved_hostnames'].get(apiHostname, apiHostname)
            for host in hosts:
                if host.hostname == apiHostname:
                    nova_api_hosts.add(host.hostname)
                    cinder_api_hosts.append(host.hostname)
                else:
                    hostIp = results['resolved_hostnames'].get(host.hostname, host.hostname)
                    if hostIp == apiIp:
                        nova_api_hosts.add(host.hostname)
                        cinder_api_hosts.append(host.hostname)
        except Exception, e:
            log.warning("Unable to perform nova-api component discovery: %s" % e)

        if not nova_api_hosts:
            log.warning("No nova-api hosts have been identified.   You must set zOpenStackNovaApiHosts to the list of hosts upon which nova-api runs.")

        for hostname in sorted(nova_api_hosts):
            title = '{0}@{1} ({2})'.format('nova-api', hostname, device.zOpenStackRegionName)
            host_id = prepId("host-{0}".format(hostname))
            nova_api_id = prepId('service-nova-api-{0}-{1}'.format(hostname, device.zOpenStackRegionName))

            services.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.NovaApi',
                data=dict(
                    id=nova_api_id,
                    title=title,
                    binary='nova-api',
                    set_hostedOn=host_id,
                    set_orgComponent=region_id
                )))

        if not cinder_api_hosts:
            log.warning("No cinder-api hosts have been identified.   You must set zOpenStackCinderApiHosts to the list of hosts upon which cinder-api runs.")

        # hosts
        for hostname in cinder_api_hosts:
            title = '{0}@{1} ({2})'.format('cinder-api', hostname, device.zOpenStackRegionName)
            host_id = prepId("host-{0}".format(hostname))
            cinder_api_id = prepId('service-cinder-api-{0}-{1}'.format(hostname, device.zOpenStackRegionName))

            services.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.CinderApi',
                data=dict(
                    id=cinder_api_id,
                    title=title,
                    binary='cinder-api',
                    set_hostedOn=host_id,
                    set_orgComponent=region_id
                )))

        # agent
        agents = []
        for agent in results['agents']:
            if not agent.get('id', None):
                continue

            sanitized_hostname = sanitize_host_or_ip(agent['host'])
            if not sanitized_hostname:
                log.debug("Skipping empty hostname %s !", agent['host'])
                continue
            if sanitized_hostname != agent['host']:
                log.debug("Sanitized hostname %s", agent['host'])

            # Get agent's host
            agent_host = prepId('host-{0}'.format(sanitized_hostname))

            # ------------------------------------------------------------------
            # AgentSubnets Section
            # ------------------------------------------------------------------

            agent_subnets = []
            agent_networks = []
            if agent.get('dhcp_agent_networks'):
                agent_networks = ['network-{0}'.format(x)
                                  for x in agent['dhcp_agent_networks']]

            if agent.get('dhcp_agent_subnets'):
                agent_subnets = ['subnet-{0}'.format(x)
                                 for x in agent['dhcp_agent_subnets']]

            if agent.get('l3_agent_subnets'):
                agent_subnets = ['subnet-{0}'.format(x)
                                 for x in agent['l3_agent_subnets']]

            if agent.get('l3_agent_networks'):
                agent_networks = ['network-{0}'.format(x)
                                  for x in agent['l3_agent_networks']]

            # ------------------------------------------------------------------
            # format l3_agent_routers
            l3_agent_routers = ['router-{0}'.format(x)
                                for x in agent['l3_agent_routers']]

            title = '{0}@{1}'.format(agent.get('agent_type', ''),
                                     agent.get('host', ''))
            agents.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.NeutronAgent',
                data=dict(
                    id=prepId('agent-{0}'.format(agent['id'])),
                    title=title,
                    binary=agent.get('binary', ''),
                    enabled=agent.get('admin_state_up', False),
                    operStatus={
                        True: 'UP',
                        False: 'DOWN'
                    }.get(agent.get('alive', None), 'UNKNOWN'),

                    agentId=agent['id'],
                    type=agent.get('agent_type', ''),

                    set_routers=l3_agent_routers,
                    set_subnets=agent_subnets,
                    set_networks=agent_networks,
                    set_hostedOn=agent_host,
                    set_orgComponent=hostmap[agent_host]['org_id'],
                )))

        # networking
        networks = []
        for net in results['networks']:
            if not net.get('id', None):
                continue

            networks.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Network',
                data=dict(
                    id=prepId('network-{0}'.format(net['id'])),
                    netId=net['id'],
                    title=net.get('name', net['id']),
                    admin_state_up=net.get('admin_state_up', False),         # true/false
                    netExternal=net.get('router:external', False),           # true/false
                    set_tenant=prepId('tenant-{0}'.format(net.get('tenant_id', ''))),
                    netStatus=net.get('status', 'UNKNOWN'),                      # ACTIVE
                    netType=net.get('provider:network_type', 'UNKNOWN').upper()  # local/global
                )))

        # subnet
        subnets = []
        for subnet in results['subnets']:
            if not subnet.get('id', None):
                continue

            subnets.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Subnet',
                data=dict(
                    cidr=subnet.get('cidr',''),
                    dns_nameservers=subnet.get('dns_nameservers', []),
                    gateway_ip=subnet.get('gateway_ip',''),
                    id=prepId('subnet-{0}'.format(subnet['id'])),
                    set_network=prepId('network-{0}'.format(subnet.get('network_id',''))),
                    set_tenant=prepId('tenant-{0}'.format(subnet.get('tenant_id',''))),
                    subnetId=subnet['id'],
                    title=subnet.get('name', subnet['id']),
                    )))

        # port
        ports = []
        device_subnet_list = defaultdict(set)
        for port in results['ports']:
            if not port.get('id', None):
                continue
            port_dict= dict()
            # Fetch the subnets for later use
            raw_subnets = get_subnets_from_fixedips(port.get('fixed_ips', []))
            port_subnets = [prepId('subnet-{}'.format(x)) for x in raw_subnets]
            if port_subnets:
                port_dict['set_subnets']= port_subnets
            # Prepare the device_subnet_list data for later use.
            port_router_id = port.get('device_id')
            if port_router_id:
                device_subnet_list[port_router_id] = \
                        device_subnet_list[port_router_id].union(raw_subnets)
            port_tenant = None
            if port.get('tenant_id', None):
                port_tenant = prepId('tenant-{0}'.format(port.get('tenant_id',
                                                                  '')))
                port_dict['set_tenant'] = port_tenant

            port_instance = get_port_instance(port.get('device_owner', ''),
                                              port.get('device_id', ''))
            if port_instance:
                port_instance = prepId(port_instance)
                port_dict['set_instance'] = port_instance
            port_network = port.get('network_id', None)
            if port_network:
                port_network = prepId('network-{0}'.format(port_network))
                port_dict['set_network'] = port_network
            ports.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Port',
                data=dict(
                    admin_state_up=port.get('admin_state_up', False),
                    device_owner=port.get('device_owner', ''),
                    fixed_ip_list=get_port_fixedips(port.get('fixed_ips', [])),
                    id=prepId('port-{0}'.format(port['id'])),
                    mac_address=port.get('mac_address', '').upper(),
                    portId=port['id'],
                    status=port.get('status', 'UNKNOWN'),
                    title=port.get('name', port['id']),
                    vif_type=port.get('binding:vif_type', 'UNKNOWN'),
                    **port_dict
                    )))

        # router
        routers = []
        for router in results['routers']:
            if not router.get('id', None):
                continue

            _gateways = set()
            _network_id = None
            # This should be all of the associated subnets
            _subnets = device_subnet_list[router['id']]

            # Get the External Gateway Data
            external_gateway_info = router.get('external_gateway_info')
            if external_gateway_info:
                _network_id = external_gateway_info.get('network_id', '')
                for _ip in external_gateway_info.get('external_fixed_ips', []):
                    _gateways.add(_ip.get('ip_address', None))
                    # This should not be required, but it doesn't hurt set()
                    _subnets.add(_ip.get('subnet_id', None))

            _network = None
            if _network_id:
                _network = 'network-{0}'.format(_network_id)

            router_dict = dict(
                admin_state_up=router.get('admin_state_up', False),
                gateways=list(_gateways),
                id=prepId('router-{0}'.format(router['id'])),
                routerId=router['id'],
                routes=list(router.get('routes', [])),
                set_tenant=prepId('tenant-{0}'.format(router.get('tenant_id',''))),
                status=router.get('status', 'UNKNOWN'),
                title=router.get('name', router['id']),
            )
            if _network:
                router_dict['set_network'] = prepId(_network)
            if _subnets:
                router_dict['set_subnets'] = [prepId('subnet-{0}'.format(x))
                                              for x in _subnets]
            routers.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Router',
                data=router_dict))

        # floatingip
        floatingips = []
        for floatingip in results['floatingips']:
            if not floatingip.get('id', None):
                continue

            floatingips.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.FloatingIp',
                data=dict(
                    floatingipId=floatingip['id'],
                    fixed_ip_address=floatingip.get('fixed_ip_address', ''),
                    floating_ip_address=floatingip.get('floating_ip_address', ''),
                    id=prepId('floatingip-{0}'.format(floatingip['id'])),
                    set_router=prepId('router-{0}'.format(floatingip.get('router_id', ''))),
                    set_network=prepId('network-{0}'.format(floatingip.get('floating_network_id', ''))),
                    set_port=prepId('port-{0}'.format(floatingip.get('port_id', ''))),
                    set_tenant=prepId('tenant-{0}'.format(floatingip.get('tenant_id',''))),
                    status=floatingip.get('status', 'UNKNOWN'),
                    )))

        # volume Types
        voltypes = []
        for voltype in results['volumetypes']:
            if not voltype.get('id'):
                continue

            voltypes.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.VolType',
                data=dict(
                    id=prepId('volType-{0}'.format(voltype['id'])),
                    title=voltype.get('name', 'UNKNOWN'),
                )))

        # volumes
        volumes = []
        for volume in results['volumes']:
            if not volume.get('id'):
                continue

            attachment = volume.get('attachments', [])
            instanceId = ''
            # each openstack volume can only attach to one instance
            if len(attachment) > 0:
                instanceId = attachment[0].get('server_id', '')

            # if not defined, volume.get('volume_type', '') returns ''
            voltypeid = [vtype.id for vtype in voltypes
                         if volume.get('volume_type', '') == vtype.title]

            volume_dict = dict(
                id=prepId('volume-{0}'.format(volume['id'])),
                title=volume.get('name', ''),
                volumeId=volume['id'],  # 847424
                avzone=volume.get('availability_zone', ''),
                created_at=volume.get('created_at', '').replace('T', ' '),
                sourceVolumeId=volume.get('source_volid', ''),
                host=volume.get('os-vol-host-attr:host', ''),
                size=volume.get('size', 0),
                bootable=volume.get('bootable', 'FALSE').upper(),
                status=volume.get('status', 'UNKNOWN').upper(),
            )
            # set tenant only when volume['attachments'] is not empty
            if len(instanceId) > 0:
                volume_dict['set_instance'] = prepId('server-{0}'.format(
                    instanceId))
            # set instance only when volume['tenant_id'] is not empty
            if volume.get('os-vol-tenant-attr:tenant_id'):
                volume_dict['set_tenant'] = prepId('tenant-{0}'.format(
                    volume.get('os-vol-tenant-attr:tenant_id','')))
            if len(voltypeid) > 0:
                volume_dict['set_volType'] = voltypeid[0]
            if volume.get('os-vol-tenant-attr:tenant_id'):
                volume_dict['set_tenant'] = prepId('tenant-{0}'.format(
                    volume.get('os-vol-tenant-attr:tenant_id')))

            volumes.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Volume',
                data=volume_dict))

        # volume Snapshots
        volsnapshots = []
        for snapshot in results['volsnapshots']:
            if not snapshot.get('id', None):
                continue

            volsnap_dict = dict(
                id=prepId('snapshot-{0}'.format(snapshot['id'])),
                title=snapshot.get('name', ''),
                created_at=snapshot.get('created_at', '').replace('T', ' '),
                size=snapshot.get('size', 0),
                description=snapshot.get('description', ''),
                status=snapshot.get('status', 'UNKNOWN').upper(),
            )
            if snapshot.get('volume_id', None):
                volsnap_dict['set_volume'] = \
                    prepId('volume-{0}'.format(snapshot.get('volume_id','')))
            if snapshot.get('os-extended-snapshot-attributes:project_id', None):
                volsnap_dict['set_tenant'] = \
                    prepId('tenant-{0}'.format(snapshot.get(
                        'os-extended-snapshot-attributes:project_id','')))
            volsnapshots.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.VolSnapshot',
                data=volsnap_dict))

        # volume backup is not ready. but also don't want to waste efforts
        # either

        # Backups
        # backups = []
        # for backup in results['backups']:
        #     if not backup.get('id', None):
        #         continue
        #
        #     backups.append(ObjectMap(
        #         modname='ZenPacks.zenoss.OpenStackInfrastructure.Backup',
        #         data=dict(
        #             snapshotId=backup['id'],
        #             id=prepId('backup-{0}'.format(backup['id'])),
        #             name=backup.get('name', ''),
        #             created_at=backup.get('created_at', ''),
        #             size=str(backup.get('size', 0)) + ' GB',
        #             description=backup.get('description', ''),
        #             snapshot=backup.get('backup_id', ''),
        #             status=backup.get('status', 'UNKNOWN').upper(),
        #             set_volume=prepId('volume-{0}'.format(backup.get('volume_id',''))),
        #             set_tenant=prepId('tenant-{0}'.format(backup.get('os-extended-snapshot-attributes:project_id',''))),
        #             )))
        #

        # Pools
        pools = []
        for pool in results['volume_pools']:
            # does pool have id?

            # pool name from Ceph looks like: 'block1@ceph#ceph
            # the right most (or the middle part?) part is volume type.
            # the middle part is Ceph cluster name?

            allocated_capacity = pool.get('capabilities', {}).get('allocated_capacity_gb', False)
            if not allocated_capacity:
                allocated_capacity = ''
            else:
                allocated_capacity = str(allocated_capacity) + ' GB',
            free_capacity = pool.get('capabilities', {}).get('free_capacity_gb', False)
            if not free_capacity:
                free_capacity = ''
            else:
                free_capacity = str(free_capacity) + ' GB',
            total_capacity = pool.get('capabilities', {}).get('total_capacity_gb', False)
            if not total_capacity:
                total_capacity = ''
            else:
                total_capacity = str(total_capacity) + ' GB',

            pools.append(ObjectMap(
                modname = 'ZenPacks.zenoss.OpenStackInfrastructure.Pool',
                data = dict(
                    #poolId=pool.get('name'),
                    id=prepId('pool-{0}'.format(pool.get('name'))),
                    title=pool.get('name', ''),
                    qos_support=pool.get('capabilities', {}).get('QoS_support', False),
                    allocated_capacity=allocated_capacity,
                    free_capacity=free_capacity,
                    total_capacity=total_capacity,
                    driver_version=pool.get('capabilities', {}).get('driver_version', ''),
                    location=pool.get('capabilities', {}).get('location_info', ''),
                    reserved_percentage=str(pool.get('capabilities', {}).get('reserved_percentage', 0)) + '%',
                    storage_protocol=pool.get('capabilities', {}).get('storage_protocol', 0),
                    vendor_name=pool.get('capabilities', {}).get('vendor_name', 0),
                    volume_backend=pool.get('capabilities', {}).get('volume_backend', 0),
                    )))

        # Quotas
        quotas = []
        for quota_key in results['quotas'].keys():
            quota = results['quotas'][quota_key]
            tenant = [t for t in quota_tenants if t['id'] == quota_key][0]

            quotas.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Quota',
                data=dict(
                    id=prepId('quota-{0}'.format(tenant['name'])),
                    tenant_name=tenant['name'],
                    volumes=quota.get('volumes', 0),
                    snapshots=quota.get('snapshots', 0),
                    bytes=quota.get('gigabytes', 0),
                    backups=quota.get('backups', 0),
                    backup_bytes=quota.get('backup_gigabytes', 0),
                    set_tenant=prepId('tenant-{0}'.format(quota_key)),
                    )))

        objmaps = {
            'flavors': flavors,
            'hosts': hosts,
            'hypervisors': hypervisors,
            'images': images,
            'regions': [region],
            'servers': servers,
            'services': services,
            'tenants': tenants,
            'zones': zones.values(),
            'agents': agents,
            'networks': networks,
            'subnets': subnets,
            'routers': routers,
            'ports': ports,
            'floatingips': floatingips,
            'volumes': volumes,
            'voltypes': voltypes,
            'volsnapshots': volsnapshots,
            # 'backups': backups,
            'pools': pools,
            'quotas': quotas,
        }

        # If we have references to tenants which we did not discover during
        # (keystone) modeling, create dummy records for them.
        all_tenant_ids = set()
        for objmap in itertools.chain.from_iterable(objmaps.values()):
            try:
                all_tenant_ids.add(objmap.set_tenant)
            except AttributeError:
                pass

        if None in all_tenant_ids:
            all_tenant_ids.remove(None)

        known_tenant_ids = set([x.id for x in tenants])
        for tenant_id in all_tenant_ids - known_tenant_ids:
            tenants.append(ObjectMap(
                modname='ZenPacks.zenoss.OpenStackInfrastructure.Tenant',
                data=dict(
                    id=prepId(tenant_id),
                    title=str(tenant_id),
                    description=str(tenant_id),
                    tenantId=tenant_id[7:]  # strip tenant- prefix
                )))

        # If the user has provided a list of static objectmaps to
        # slap on the ends of the ones we discovered dynamically, add them in.
        # (this is mostly for testing purposes!)
        filename = zenpack_path('static_objmaps.json')
        if os.path.exists(filename):
            log.info("Loading %s" % filename)
            data = ''
            with open(filename) as f:
                for line in f:
                    # skip //-style comments
                    if re.match(r'^\s*//', line):
                        data += "\n"
                    else:
                        data += line
                static_objmaps = json.loads(data)
            for key in objmaps:
                if key in static_objmaps and len(static_objmaps[key]):
                    starting_count = len(objmaps[key])
                    for om_dict in static_objmaps[key]:
                        compname = om_dict.pop('compname', None)
                        modname = om_dict.pop('modname', None)
                        classname = om_dict.pop('classname', None)
                        for v in om_dict:
                            om_dict[v] = str(om_dict[v])

                        if 'id' not in om_dict:
                            # Try to match it to an existing objectmap by title (as a regexp)
                            # and merge into it.
                            found = False
                            for om in objmaps[key]:
                                if re.match(om_dict['title'], om.title):
                                    found = True
                                    for attr in om_dict:
                                        if attr != 'title':
                                            log.info("  Adding %s=%s to %s (%s)" % (attr, om_dict[attr], om.id, om.title))
                                            setattr(om, attr, om_dict[attr])
                                    break

                            if not found:
                                log.error("Unable to find a matching objectmap to extend: %s" % om_dict)

                            continue

                        objmaps[key].append(ObjectMap(compname=compname,
                                                      modname=modname,
                                                      classname=classname,
                                                      data=om_dict))
                    added_count = len(objmaps[key]) - starting_count
                    if added_count > 0:
                        log.info("  Added %d new objectmaps to %s" % (added_count, key))

        # Apply the objmaps in the right order.
        componentsMap = RelationshipMap(relname='components')
        for i in ('tenants', 'regions', 'flavors', 'images', 'servers', 'zones',
                  'hosts', 'hypervisors', 'services', 'networks',
                  'subnets', 'routers', 'ports', 'agents', 'floatingips',
                  'voltypes', 'volumes', 'volsnapshots', 'pools', 'quotas',
                  ):
            for objmap in objmaps[i]:
                componentsMap.append(objmap)

        endpointObjMap = ObjectMap(
            modname='ZenPacks.zenoss.OpenStackInfrastructure.Endpoint',
            data=dict(
                set_maintain_proxydevices=True
            )
        )

        return (componentsMap, endpointObjMap)
