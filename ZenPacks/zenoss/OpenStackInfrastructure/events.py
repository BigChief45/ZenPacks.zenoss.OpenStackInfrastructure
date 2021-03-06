##############################################################################
#
# Copyright (C) Zenoss, Inc. 2014, all rights reserved.
#
# This content is made available according to terms specified in
# License.zenoss under the directory where your Zenoss product is installed.
#
##############################################################################

from Products.DataCollector.plugins.DataMaps import ObjectMap
from Products.DataCollector.ApplyDataMap import ApplyDataMap
from Products.ZenUtils.Utils import prepId
from ZenPacks.zenoss.OpenStackInfrastructure.utils import (get_subnets_from_fixedips,
                                                           get_port_instance,
                                                           getNetSubnetsGws_from_GwInfo,
                                                           get_port_fixedips,
                                                           is_uuid,
                                                           )
import ast

import logging
LOG = logging.getLogger('zen.OpenStack.events')


# Sets of traits we can expect to get (see event_definitions.yaml on the
# openstack side) and what objmap properties they map to.
NEUTRON_TRAITMAPS = {
    'floatingip': {
        'fixed_ip_address':          ['fixed_ip_address'],
        'floating_ip_address':       ['floating_ip_address'],
        'id':                        ['floatingipId'],
        'status':                    ['status'],
        # See: _apply_neutron_traits: set_tenant
        # _apply_trait_rel:  set_network, set_port, set_router,
        # set_network(floating_network_id)
    },
    'network': {
        'admin_state_up':            ['admin_state_up'],
        'id':                        ['netId'],
        'name':                      ['title'],
        'provider_network_type':     ['netType'],
        'router_external':           ['netExternal'],
        'status':                    ['netStatus'],
        # See: _apply_neutron_traits: set_tenant
    },
    'port': {
        'admin_state_up':            ['admin_state_up'],
        'binding:vif_type':          ['vif_type'],
        'device_owner':              ['device_owner'],
        'id':                        ['portId'],
        'mac_address':               ['mac_address'],
        'name':                      ['title'],
        'status':                    ['status'],
        # See: _apply_neutron_traits: set_tenant, set_network
    },
    'router': {
        'admin_state_up':            ['admin_state_up'],
        'id':                        ['routerId'],
        'routes':                    ['routes'],
        'status':                    ['status'],
        'name':                      ['title'],
        # See: _apply_router_gateway_info:
        # (gateways, set_subnets, set_network)
    },
    'security_group': {
        'id':                        ['sgId'],
        'name':                      ['title'],
    },
    'subnet': {
        'cidr':                      ['cidr'],
        'gateway_ip':                ['gateway_ip'],
        'id':                        ['subnetId'],
        'name':                      ['title'],
        'network_id':                ['subnetId'],
        # See: _apply_dns_info(): dns_nameservers
        # _apply_neutron_traits: set_tenant, set_network,
    },
}

# -----------------------------------------------------------------------------
# ID Functions
# -----------------------------------------------------------------------------
def make_id(prefix, raw_id):
    """Return a valid id in "<prefix>-<raw_id>" format"""
    if not raw_id:
        LOG.warning("Missing data for %s Id" % prefix)
        return None
    return prepId("{0}-{1}".format(prefix,raw_id))

def instance_id(evt):
    if hasattr(evt, 'trait_instance_id'):
        return make_id('server', evt.trait_instance_id)

def floatingip_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('floatingip', evt.trait_id)

def network_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('network', evt.trait_id)

def port_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('port', evt.trait_id)

def router_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('router', evt.trait_id)

def securitygroup_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('securitygroup', evt.trait_id)

def subnet_id(evt):
    if hasattr(evt, 'trait_id'):
        return make_id('subnet', evt.trait_id)

def tenant_id(evt):
    if hasattr(evt, 'trait_tenant_id'):
        return make_id('tenant', evt.trait_tenant_id)

def volume_id(evt):
    if hasattr(evt, 'trait_volume_id'):
        return make_id('volume', evt.trait_volume_id)

def volsnapshot_id(evt):
    if hasattr(evt, 'trait_snapshot_id'):
        return make_id('volsnapshot', evt.trait_snapshot_id)

# -----------------------------------------------------------------------------
# Traitmap Functions
# -----------------------------------------------------------------------------
def _apply_neutron_traits(evt, objmap, traitset):
    traitmap = NEUTRON_TRAITMAPS[traitset]

    for trait in traitmap:
        for prop_name in traitmap[trait]:
            trait_field = 'trait_' + trait
            if hasattr(evt, trait_field):
                # Cast trait_admin_state_up to boolean for renderers
                if trait_field == 'trait_admin_state_up':
                    value = str(getattr(evt, 'trait_admin_state_up')).lower() == 'true'
                else:
                    value = getattr(evt, trait_field)
                setattr(objmap, prop_name, value)

    # Set the Tenant ID
    if hasattr(evt, 'trait_tenant_id'):
        setattr(objmap, 'set_tenant', tenant_id(evt))

def _apply_trait_rel(evt, objmap, trait_name, class_rel):
    ''' Generic: Set the class relation's set_* attribute: (ex: set_network)
        Ex: _apply_trait_rel(evt, objmap, 'trait_network_id', 'network')
    '''
    if hasattr(evt, trait_name):
        attrib_id = make_id(class_rel, getattr(evt, trait_name))
        # for volume attaching to an instance, we do:
        # volume['set_instance'] = 'server-<instance id>'
        if 'instance' in trait_name and 'server' in class_rel:
            # volume attaching to an instance
            setattr(objmap, 'set_instance', attrib_id)
        else:
            set_name = 'set_' + class_rel
            setattr(objmap, set_name, attrib_id)

def _apply_router_gateway_info(evt, objmap):
    ''' Get the router gateways, network, and subnets'''
    if hasattr(evt, 'trait_external_gateway_info'):

        ext_gw_info = ast.literal_eval(evt.trait_external_gateway_info)
        if ext_gw_info:
            gateways = set()
            subnets = set()
            (network, subnets, gateways) = getNetSubnetsGws_from_GwInfo(ext_gw_info)

            net_id = make_id('network', network)
            subnet_ids = ['subnet-{0}'.format(x) for x in subnets]

            setattr(objmap, 'gateways', list(gateways))
            setattr(objmap, 'set_network', net_id)
            setattr(objmap, 'set_subnets', subnet_ids)

def _apply_dns_info(evt, objmap):
    ''' Get the dns servers for subnets as a string'''
    if hasattr(evt, 'trait_dns_nameservers'):
        dns_info = ast.literal_eval(evt.trait_dns_nameservers)
        servers = ", ".join(dns_info)
        setattr(objmap, 'dns_nameservers', servers)

def _apply_instance_traits(evt, objmap):
    traitmap = {
                'display_name': ['title', 'hostName'],
                'instance_id':  ['resourceId', 'serverId'],
                'state':        ['serverStatus'],
                'flavor_name':  ['set_flavor_name'],
                'host_name':    ['set_host_name'],
                'image_name':   ['set_image_name'],
                'tenant_id':    ['set_tenant_id']
               }
    for trait in traitmap:
        for prop_name in traitmap[trait]:
            trait_field = 'trait_' + trait
            if hasattr(evt, trait_field):
                value = getattr(evt, trait_field)

                # Store server status in uppercase, to match how the nova-api
                # shows it.
                if prop_name == 'serverStatus':
                    value = value.upper()

                setattr(objmap, prop_name, value)

    # special case for publicIps / privateIps
    if hasattr(evt, 'trait_fixed_ips'):
        try:
            fixed_ips = ast.literal_eval(evt.trait_fixed_ips)
            public_ips = set()
            private_ips = set()
            # Assume: Fixed_ips are private, floating_ips are external/public:
            for ip in fixed_ips:
                private_ips.add(ip['address'])
                for fip in ip['floating_ips']:
                    public_ips.add(fip)
            setattr(objmap, 'privateIps', list(private_ips))
            # public_ips may not be listed, so don't delete existing ones.
            if public_ips:
                setattr(objmap, 'publicIps', list(public_ips))
        except Exception, e:
            LOG.debug("Unable to parse trait_fixed_ips=%s (%s)" % (evt.trait_fixed_ips, e))


def _apply_cinder_traits(evt, objmap):
    traitmap = {
                'status':        ['status'],
                'display_name': ['title'],
                'availability_zone':   ['avzone'],
                'created_at':   ['created_at'],
                'volume_id':  ['volumeId'],
                'host':    ['host'],
                'type':  ['volume_type'],
                'size':  ['size'],
               }
    if 'VolSnapshot' in objmap.modname:
        # volume snapshot does not have volume_id, only volume does
        traitmap.pop('volume_id', None)
    for trait in traitmap:
        for prop_name in traitmap[trait]:
            trait_field = 'trait_' + trait
            if hasattr(evt, trait_field):
                value = getattr(evt, trait_field)
                # awkward!
                if prop_name == 'status':
                    value = value.upper()
                setattr(objmap, prop_name, value)


def instance_objmap(evt):
    return ObjectMap(
        modname='ZenPacks.zenoss.OpenStackInfrastructure.Instance',
        compname='',
        data={
            'id': instance_id(evt),
            'relname': 'components'
        },
    )

def neutron_objmap(evt, Name):
    """ Create an object map of type Name. Name must be proper module name.
        WARNING: All Neutron events have a 'trait_id' attribute.
                 Make sure that Name.lower() corresponds to a well defined
                 id_function. Especially SecurityGroups!
    """
    module = 'ZenPacks.zenoss.OpenStackInfrastructure.' + Name
    id_func = eval(Name.lower() + '_id')
    _id = id_func(evt)

    return ObjectMap(
        modname=module,
        compname='',
        data={'id': _id,
              'relname': 'components'
              },
    )

def cinder_objmap(evt, Name):
    """ Create an object map of type Name. Name must be proper module name.
    """
    module = 'ZenPacks.zenoss.OpenStackInfrastructure.' + Name
    id_func = eval(Name.lower() + '_id')
    _id = id_func(evt)

    return ObjectMap(
        modname=module,
        compname='',
        data={'id': _id,
              'relname': 'components'
              },
    )

def event_summary(component_name, evt):
    """ Gives correct summary for Create/Update event messages
    """
    if '.create' in evt.eventClassKey:
        action = "Created"
    else:
        action = "Updated"
    return "%s %s %s" % (action, component_name, evt.trait_id)

# -----------------------------------------------------------------------------
# Event Functions
# -----------------------------------------------------------------------------
def instance_create(device, dmd, evt):
    evt.summary = "Instance %s created" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_update(device, dmd, evt):
    evt.summary = "Instance %s updated" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_delete(device, dmd, evt):
    evt.summary = "Instance %s deleted" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    objmap.remove = True
    return [objmap]

def instance_update_status(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s updated" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def add_statuschange_to_msg(evt, msg):
    descr = ''
    if hasattr(evt, 'trait_state_description') and evt.trait_state_description:
        descr = ' [%s]' % evt.trait_state_description

    if hasattr(evt, 'trait_old_state'):
        msg = msg + " (status changed from %s to %s%s)" % (
            evt.trait_old_state, evt.trait_state, descr)
    elif hasattr(evt, 'trait_state'):
        msg = msg + " (status changed to %s%s)" % (
            evt.trait_state, descr)

    return msg

def instance_powering_on(device, dmd, evt):
    evt.summary = "Instance %s powering on" % (evt.trait_display_name)
    return []

def instance_powered_on(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s powered on" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_powering_off(device, dmd, evt):
    evt.summary = "Instance %s powering off" % (evt.trait_display_name)
    return []

def instance_powered_off(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s powered off" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_shutting_down(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s shutting down" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_shut_down(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s shut down" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_rebooting(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s rebooting" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_rebooted(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s rebooted" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_rebuilding(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s rebuilding" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_rebuilt(device, dmd, evt):
    evt.summary = add_statuschange_to_msg(
            evt,
            "Instance %s rebuilt" % (evt.trait_display_name)
            )

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_suspended(device, dmd, evt):
    evt.summary = "Instance %s suspended" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_resumed(device, dmd, evt):
    evt.summary = "Instance %s resumed" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_rescue(device, dmd, evt):
    evt.summary = "Instance %s placed in rescue mode" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_unrescue(device, dmd, evt):
    evt.summary = "Instance %s removed from rescue mode" % (evt.trait_display_name)

    objmap = instance_objmap(evt)
    _apply_instance_traits(evt, objmap)
    return [objmap]

def instance_volume_attach(device, dmd, evt):
    evt.summary = "Instance %s being attached to a volume %s" % \
        (evt.trait_display_name, evt.trait_volume_id)

    # volume_update() for volume.attach.end will take care of it.
    return []
    
def instance_volume_detach(device, dmd, evt):
    evt.summary = "Instance %s being detached from a volume %s" % \
        (evt.trait_display_name, evt.trait_volume_id)

    # volume_update() for volume.detach.end will take care of it.
    return []
    

# -----------------------------------------------------------------------------
# FloatingIp
# -----------------------------------------------------------------------------
def floatingip_create_start(device, dmd, evt):
    evt.summary = "Creating FloatingIp %s" % (evt.trait_id)
    return []

def floatingip_update_start(device, dmd, evt):
    evt.summary = "Updating FloatingIp %s" % (evt.trait_id)
    return []

def floatingip_update(device, dmd, evt):
    evt.summary = event_summary("FloatingIp", evt)

    objmap = neutron_objmap(evt, "FloatingIp")
    _apply_neutron_traits(evt, objmap, 'floatingip')

    _apply_trait_rel(evt, objmap, 'trait_floating_network_id', 'network')
    _apply_trait_rel(evt, objmap, 'trait_router_id', 'router')
    _apply_trait_rel(evt, objmap, 'trait_port_id', 'port')
    return [objmap]

def floatingip_delete_start(device, dmd, evt):
    evt.summary = "Deleting FloatingIp %s " % (evt.trait_id)
    return []

def floatingip_delete_end(device, dmd, evt):
    evt.summary = "FloatingIp %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'FloatingIp')
    objmap.remove = True
    return [objmap]

# -----------------------------------------------------------------------------
# Network Event Functions
# -----------------------------------------------------------------------------
def network_create_start(device, dmd, evt):
    evt.summary = "Creating Network %s" % (evt.trait_name)
    return []

def network_update_start(device, dmd, evt):
    evt.summary = "Updating Network %s" % (evt.trait_id)
    return []

def network_update(device, dmd, evt):
    evt.summary = event_summary("Network", evt)

    objmap = neutron_objmap(evt, "Network")
    _apply_neutron_traits(evt, objmap, 'network')
    return [objmap]

def network_delete_start(device, dmd, evt):
    evt.summary = "Deleting Network %s " % (evt.trait_id)
    return []

def network_delete_end(device, dmd, evt):
    evt.summary = "Network %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'Network')
    objmap.remove = True
    return [objmap]

# -----------------------------------------------------------------------------
# Port Event Functions
# -----------------------------------------------------------------------------
def port_create_start(device, dmd, evt):
    evt.summary = "Creating Port %s" % (evt.trait_id)
    return []

def port_update_start(device, dmd, evt):
    evt.summary = "Updating Port %s" % (evt.trait_id)
    return []

def port_update(device, dmd, evt):
    evt.summary = event_summary("Port", evt)

    objmap = neutron_objmap(evt, "Port")
    _apply_neutron_traits(evt, objmap, 'port')
    _apply_trait_rel(evt, objmap, 'trait_network_id', 'network')

    # If device_owner is part of compute, then add device_id as set_instance
    if 'compute' in evt.trait_device_owner and evt.trait_device_id:
        _apply_trait_rel(evt, objmap, 'trait_device_id', 'server')

    if hasattr(evt, 'evt.trait_device_id'):
        port_instance = get_port_instance(evt.trait_device_owner,
                                           evt.trait_device_id)
        setattr(objmap, 'set_instance', port_instance)

    # get subnets and fixed_ips
    if hasattr(evt, 'trait_fixed_ips'):
        port_fips = ast.literal_eval(evt.trait_fixed_ips)
        _subnets = get_subnets_from_fixedips(port_fips)
        port_subnets = [prepId('subnet-{}'.format(x)) for x in _subnets]
        port_fixedips = get_port_fixedips(port_fips)
        setattr(objmap, 'set_subnets', port_subnets)
        setattr(objmap, 'fixed_ip_list', port_fixedips)

    return [objmap]

def port_delete_start(device, dmd, evt):
    evt.summary = "Deleting Port %s " % (evt.trait_id)
    return []

def port_delete_end(device, dmd, evt):
    evt.summary = "Port %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'Port')
    objmap.remove = True
    return [objmap]

# -----------------------------------------------------------------------------
# Router Event Functions
# -----------------------------------------------------------------------------
def router_create_start(device, dmd, evt):
    evt.summary = "Creating Router %s" % (evt.trait_id)
    return []

def router_update_start(device, dmd, evt):
    evt.summary = "Updating Router %s" % (evt.trait_id)
    return []

def router_update(device, dmd, evt):
    evt.summary = event_summary("Router", evt)

    objmap = neutron_objmap(evt, "Router")
    _apply_neutron_traits(evt, objmap, 'router')
    _apply_router_gateway_info(evt, objmap)
    return [objmap]

def router_delete_start(device, dmd, evt):
    evt.summary = "Deleting Router %s " % (evt.trait_id)
    return []

def router_delete_end(device, dmd, evt):
    evt.summary = "Router %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'Router')
    objmap.remove = True
    return [objmap]

# -----------------------------------------------------------------------------
# SecurityGroup Event Functions: Carefull with the underscore differences
# -----------------------------------------------------------------------------
def securityGroup_create_start(device, dmd, evt):
    evt.summary = "Creating SecurityGroup %s" % (evt.trait_id)
    return []

def securityGroup_update_start(device, dmd, evt):
    evt.summary = "Updating SecurityGroup %s" % (evt.trait_id)
    return []

def securityGroup_update(device, dmd, evt):
    evt.summary = event_summary("SecurityGroup", evt)

    objmap = neutron_objmap(evt, "SecurityGroup")
    _apply_neutron_traits(evt, objmap, 'security_group')
    return [objmap]

def securityGroup_delete_start(device, dmd, evt):
    evt.summary = "Deleting SecurityGroup %s " % (evt.trait_id)
    return []

def securityGroup_delete_end(device, dmd, evt):
    evt.summary = "SecurityGroup %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'SecurityGroup')
    objmap.remove = True
    return [objmap]

# -----------------------------------------------------------------------------
# Subnet Event Functions
# -----------------------------------------------------------------------------
def subnet_create_start(device, dmd, evt):
    evt.summary = "Creating Subnet %s" % (evt.trait_id)
    return []

def subnet_update_start(device, dmd, evt):
    evt.summary = "Updating Subnet %s" % (evt.trait_id)
    return []

def subnet_update(device, dmd, evt):
    evt.summary = event_summary("Subnet", evt)

    objmap = neutron_objmap(evt, "Subnet")
    _apply_dns_info(evt, objmap)
    _apply_neutron_traits(evt, objmap, 'subnet')
    _apply_trait_rel(evt, objmap, 'trait_network_id', 'network')
    return [objmap]

def subnet_delete_start(device, dmd, evt):
    evt.summary = "Deleting Subnet %s " % (evt.trait_id)
    return []

def subnet_delete_end(device, dmd, evt):
    evt.summary = "Subnet %s deleted" % (evt.trait_id)

    objmap = neutron_objmap(evt, 'Subnet')
    objmap.remove = True
    return [objmap]


# -----------------------------------------------------------------------------
# Volume Event Functions
# -----------------------------------------------------------------------------
def volume_create_start(device, dmd, evt):
    evt.summary = "Creating Volume %s" % (evt.trait_volume_id)
    return []

def volume_update_start(device, dmd, evt):
    evt.summary = "Updating Volume %s" % (evt.trait_volume_id)
    return []

def volume_update(device, dmd, evt):
    if 'create.end' in evt.eventClassKey:
        # the volume is being created
        evt.summary = "Created Volume %s" % (evt.trait_volume_id)
    elif 'attach.end' in evt.eventClassKey:
        # the volume is being attached to an instance
        evt.summary = "Attach Volume %s to instance %s " % \
            (evt.trait_volume_id, evt.trait_instance_id)
    elif 'detach.end' in evt.eventClassKey:
        # the volume is being detached from an instance
        evt.summary = "Detach Volume %s from instance" % \
            (evt.trait_volume_id)

    objmap = cinder_objmap(evt, "Volume")
    _apply_dns_info(evt, objmap)
    _apply_cinder_traits(evt, objmap)
    _apply_trait_rel(evt, objmap, 'trait_tenant_id', 'tenant')
    if hasattr(objmap, 'instanceId') and len(objmap.instanceId) > 0:
        # the volume is being attached to an instance
        _apply_trait_rel(evt, objmap, 'trait_instance_id', 'server')
    elif 'detach.end' in evt.eventClassKey:
        # the volume is being detached from an instance
        setattr(objmap, 'set_instance', '')
    # make sure objmap has volume_type and volume_type is uuid
    if hasattr(objmap, 'volume_type') and is_uuid(objmap.volume_type):
        # set volume type
        _apply_trait_rel(evt, objmap, 'trait_type', 'volType')
        
    return [objmap]

def volume_delete_start(device, dmd, evt):
    evt.summary = "Deleting Volume %s " % (evt.trait_volume_id)
    return []

def volume_delete_end(device, dmd, evt):
    evt.summary = "Volume %s deleted" % (evt.trait_volume_id)

    objmap = cinder_objmap(evt, 'Volume')
    objmap.remove = True
    return [objmap]

def volume_attach_start(device, dmd, evt):
    evt.summary = "Attaching Volume %s to instance" %  (evt.trait_display_name)
    return []

def volume_detach_start(device, dmd, evt):
    evt.summary = "Detaching Volume %s from instance %s" % \
        (evt.trait_display_name, evt.trait_instance_id)
    return []



# -----------------------------------------------------------------------------
# Snapshot Event Functions
# -----------------------------------------------------------------------------
def volsnapshot_create_start(device, dmd, evt):
    evt.summary = "Creating Volume Snapshot %s for volume %s" % \
        (evt.trait_snapshot_id, evt.trait_volume_id)
    return []

def volsnapshot_update(device, dmd, evt):
    evt.summary = "Created Volume Snapshot %s for volume %s" % \
        (evt.trait_snapshot_id, evt.trait_volume_id)

    objmap = cinder_objmap(evt, "VolSnapshot")
    _apply_dns_info(evt, objmap)
    _apply_cinder_traits(evt, objmap)
    _apply_trait_rel(evt, objmap, 'trait_tenant_id', 'tenant')
    _apply_trait_rel(evt, objmap, 'trait_volume_id', 'volume')
    return [objmap]

def volsnapshot_delete_start(device, dmd, evt):
    evt.summary = "Deleting Snapshot %s for volume %s" % \
        (evt.trait_snapshot_id, evt.trait_volume_id)
    return []

def volsnapshot_delete_end(device, dmd, evt):
    evt.summary = "Volume Snapshot %s deleted for volume %s" % \
        (evt.trait_snapshot_id, evt.trait_volume_id)

    objmap = cinder_objmap(evt, 'VolSnapshot')
    objmap.remove = True
    return [objmap]


# For each eventClassKey, associate it with the appropriate mapper function.
# A mapper function is expected to take an event and return one or more objmaps.
# it may also modify the event, for instance by add missing information
# such as a summary.

MAPPERS = {
    'openstack|compute.instance.create.start':     (instance_id, instance_create),
    'openstack|compute.instance.create.end':       (instance_id, instance_update),
    'openstack|compute.instance.create.error':     (instance_id, instance_update),
    'openstack|compute.instance.update':           (instance_id, instance_update_status),
    'openstack|compute.instance.delete.start':     (instance_id, None),
    'openstack|compute.instance.delete.end':       (instance_id, instance_delete),

    'openstack|compute.instance.create_ip.start':  (instance_id, None),
    'openstack|compute.instance.create_ip.end':    (instance_id, None),
    'openstack|compute.instance.delete_ip.start':  (instance_id, None),
    'openstack|compute.instance.delete_ip.end':    (instance_id, None),

    'openstack|compute.instance.exists':              (instance_id, None),
    'openstack|compute.instance.exists.verified.old': (instance_id, None),

    # Note: I do not currently have good test data for what a real
    # live migration looks like.  I am assuming that the new host will be
    # carried in the last event, and only processing that one.
    'openstack|compute.instance.live_migration.pre.start': (instance_id, None),
    'openstack|compute.instance.live_migration.pre.end': (instance_id, None),
    'openstack|compute.instance.live_migration.post.dest.start': (instance_id, None),
    'openstack|compute.instance.live_migration.post.dest.end':  (instance_id, None),
    'openstack|compute.instance.live_migration._post.start':  (instance_id, None),
    'openstack|compute.instance.live_migration._post.end': (instance_id, instance_update),

    'openstack|compute.instance.power_off.start':  (instance_id, instance_powering_off),
    'openstack|compute.instance.power_off.end':    (instance_id, instance_powered_off),
    'openstack|compute.instance.power_on.start':   (instance_id, instance_powering_on),
    'openstack|compute.instance.power_on.end':     (instance_id, instance_powered_on),
    'openstack|compute.instance.reboot.start':     (instance_id, instance_rebooting),
    'openstack|compute.instance.reboot.end':       (instance_id, instance_rebooted),
    'openstack|compute.instance.shutdown.start':   (instance_id, instance_shutting_down),
    'openstack|compute.instance.shutdown.end':     (instance_id, instance_shut_down),
    'openstack|compute.instance.rebuild.start':    (instance_id, instance_rebuilding),
    'openstack|compute.instance.rebuild.end':      (instance_id, instance_rebuilt),

    'openstack|compute.instance.rescue.start':     (instance_id, None),
    'openstack|compute.instance.rescue.end':       (instance_id, instance_rescue),
    'openstack|compute.instance.unrescue.start':   (instance_id, None),
    'openstack|compute.instance.unrescue.end':     (instance_id, instance_unrescue),

    'openstack|compute.instance.finish_resize.start':  (instance_id, None),
    'openstack|compute.instance.finish_resize.end':    (instance_id, instance_update),
    'openstack|compute.instance.resize.start':         (instance_id, None),
    'openstack|compute.instance.resize.confirm.start': (instance_id, None),
    'openstack|compute.instance.resize.confirm.end':   (instance_id, None),
    'openstack|compute.instance.resize.prep.end':      (instance_id, None),
    'openstack|compute.instance.resize.prep.start':    (instance_id, None),
    'openstack|compute.instance.resize.revert.end':    (instance_id, instance_update),
    'openstack|compute.instance.resize.revert.start':  (instance_id, None),
    'openstack|compute.instance.resize.end':           (instance_id, instance_update),

    'openstack|compute.instance.snapshot.end':    (instance_id, None),
    'openstack|compute.instance.snapshot.start':  (instance_id, None),

    'openstack|compute.instance.suspend':         (instance_id, instance_suspended),
    'openstack|compute.instance.resume':          (instance_id, instance_resumed),

    'openstack|compute.instance.volume.attach':   (instance_id, instance_volume_attach),
    'openstack|compute.instance.volume.detach':   (instance_id, instance_volume_detach),

    # -------------------------------------------------------------------------
    # DHCP Agent
    # -------------------------------------------------------------------------
    'openstack|dhcp_agent.network.add':       (None, None),
    'openstack|dhcp_agent.network.remove':    (None, None),

    # -------------------------------------------------------------------------
    #  Firewalls --------------------------------------------------------------
    # -------------------------------------------------------------------------
    # Firewall
    'openstack|firewall.create.start':        (None, None),
    'openstack|firewall.create.end':          (None, None),
    'openstack|firewall.update.start':        (None, None),
    'openstack|firewall.update.end':          (None, None),
    'openstack|firewall.delete.start':        (None, None),
    'openstack|firewall.delete.end':          (None, None),

    # firewall_policy
    'openstack|firewall_policy.create.start': (None, None),
    'openstack|firewall_policy.create.end':   (None, None),
    'openstack|firewall_policy.update.start': (None, None),
    'openstack|firewall_policy.update.end':   (None, None),
    'openstack|firewall_policy.delete.start': (None, None),
    'openstack|firewall_policy.delete.end':   (None, None),

    # firewall_rule
    'openstack|firewall_rule.create.start':   (None, None),
    'openstack|firewall_rule.create.end':     (None, None),
    'openstack|firewall_rule.update.start':   (None, None),
    'openstack|firewall_rule.update.end':     (None, None),
    'openstack|firewall_rule.delete.start':   (None, None),
    'openstack|firewall_rule.delete.end':     (None, None),

    # -------------------------------------------------------------------------
    #  Floating IP's
    # -------------------------------------------------------------------------
    'openstack|floatingip.create.start':     (floatingip_id, floatingip_create_start),
    'openstack|floatingip.create.end':       (floatingip_id, floatingip_update),
    'openstack|floatingip.update.start':     (floatingip_id, floatingip_update),
    'openstack|floatingip.update.end':       (floatingip_id, floatingip_update),
    'openstack|floatingip.delete.start':     (floatingip_id, floatingip_delete_start),
    'openstack|floatingip.delete.end':       (floatingip_id, floatingip_delete_end),

    # -------------------------------------------------------------------------
    #  Network
    # -------------------------------------------------------------------------
    'openstack|network.create.start':        (None, network_create_start),
    'openstack|network.create.end':          (network_id, network_update),
    'openstack|network.update.start':        (network_id, network_update_start),
    'openstack|network.update.end':          (network_id, network_update),
    'openstack|network.delete.start':        (network_id, network_delete_start),
    'openstack|network.delete.end':          (network_id, network_delete_end),

    # -------------------------------------------------------------------------
    #  Port
    # -------------------------------------------------------------------------
    'openstack|port.create.start':           (port_id, port_create_start),
    'openstack|port.create.end':             (port_id, port_update),
    'openstack|port.update.start':           (port_id, port_update_start),
    'openstack|port.update.end':             (port_id, port_update),
    'openstack|port.delete.start':           (port_id, port_delete_start),
    'openstack|port.delete.end':             (port_id, port_delete_end),

    # -------------------------------------------------------------------------
    #  Routers
    # -------------------------------------------------------------------------
    'openstack|router.create.start':         (router_id, router_create_start),
    'openstack|router.create.end':           (router_id, router_update),
    'openstack|router.update.start':         (router_id, router_update_start),
    'openstack|router.update.end':           (router_id, router_update),
    'openstack|router.delete.start':         (router_id, router_delete_start),
    'openstack|router.delete.end':           (router_id, router_delete_end),

    'openstack|router.interface.create':        (None, None),
    'openstack|router.interface.update':        (None, None),
    'openstack|router.interface.delete':        (None, None),

    # -------------------------------------------------------------------------
    #  Security_group
    # -------------------------------------------------------------------------
    # 'openstack|security_group.create.start': (securitygroup_id, securityGroup_create_start),
    # 'openstack|security_group.create.end':   (securitygroup_id, securityGroup_update),
    # 'openstack|security_group.update.start': (securitygroup_id, securityGroup_update_start),
    # 'openstack|security_group.update.end':   (securitygroup_id, securityGroup_update),
    # 'openstack|security_group.delete.start': (securitygroup_id, securityGroup_delete_start),
    # 'openstack|security_group.delete.end':   (securitygroup_id, securityGroup_delete_end),

    'openstack|security_group.create.start':     (None, None),
    'openstack|security_group.create.end':       (None, None),
    'openstack|security_group.update.start':     (None, None),
    'openstack|security_group.update.end':       (None, None),
    'openstack|security_group.delete.start':     (None, None),
    'openstack|security_group.delete.end':       (None, None),

    # -------------------------------------------------------------------------
    #  Security_group_rule
    # -------------------------------------------------------------------------
    'openstack|security_group_rule.create.start': (None, None),
    'openstack|security_group_rule.create.end':   (None, None),
    'openstack|security_group_rule.update.start': (None, None),
    'openstack|security_group_rule.update.end':   (None, None),
    'openstack|security_group_rule.delete.start': (None, None),
    'openstack|security_group_rule.delete.end':   (None, None),

    # -------------------------------------------------------------------------
    #  Subnet
    # -------------------------------------------------------------------------
    'openstack|subnet.create.start':         (subnet_id, subnet_create_start),
    'openstack|subnet.create.end':           (subnet_id, subnet_update),
    'openstack|subnet.update.start':         (subnet_id, subnet_update_start),
    'openstack|subnet.update.end':           (subnet_id, subnet_update),
    'openstack|subnet.delete.start':         (subnet_id, subnet_delete_start),
    'openstack|subnet.delete.end':           (subnet_id, subnet_delete_end),

    # -------------------------------------------------------------------------
    #  Volume
    #
    # volume attaching to instance job flow:
    # 1. volume.attach.start
    # 2. volume.attach.end
    # 3. compute.instance.volume.attach
    # volume detaching from instance job flow:
    # 1. compute.instance.volume.detach
    # 2. volume.detach.start
    # 3. volume.detach.end
    # -------------------------------------------------------------------------
    'openstack|volume.create.start':         (volume_id, volume_create_start),
    'openstack|volume.create.end':           (volume_id, volume_update),
    'openstack|volume.update.start':         (volume_id, volume_update_start),
    'openstack|volume.update.end':           (volume_id, volume_update),
    'openstack|volume.delete.start':         (volume_id, volume_delete_start),
    'openstack|volume.delete.end':           (volume_id, volume_delete_end),
    'openstack|volume.attach.start':         (volume_id, volume_attach_start),
    'openstack|volume.attach.end':           (volume_id, volume_update),
    'openstack|volume.detach.start':         (volume_id, volume_detach_start),
    'openstack|volume.detach.end':           (volume_id, volume_update),

    # -------------------------------------------------------------------------
    #  Snapshot
    # -------------------------------------------------------------------------
    'openstack|snapshot.create.start':         (volsnapshot_id, volsnapshot_create_start),
    'openstack|snapshot.create.end':           (volsnapshot_id, volsnapshot_update),
    'openstack|snapshot.delete.start':         (volsnapshot_id, volsnapshot_delete_start),
    'openstack|snapshot.delete.end':           (volsnapshot_id, volsnapshot_delete_end),

}


# ==============================================================================
#  This is the main process() function that is called by the Events Transform
#  subsystem. See objects.xml for further detail.
# ==============================================================================

def process(evt, device, dmd, txnCommit):
    (idfunc, mapper) = MAPPERS.get(evt.eventClassKey, (None, None))

    if idfunc:
        evt.component = idfunc(evt)

    if mapper:
        datamaps = mapper(device, dmd, evt)
        if datamaps:
            adm = ApplyDataMap(device)
            for datamap in datamaps:
                # LOG.debug("Applying %s" % datamap)
                adm._applyDataMap(device, datamap)

        return len(datamaps)
    else:
        return 0
