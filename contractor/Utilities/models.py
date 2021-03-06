import re
import random
from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError, ObjectDoesNotExist

from cinp.orm_django import DjangoCInP as CInP

from contractor.fields import MapField, IpAddressField, hostname_regex, name_regex
from contractor.BluePrint.models import PXE
from contractor.Site.models import Site
from contractor.lib.ip import IpIsV4, CIDRNetworkBounds, StrToIp, IpToStr, CIDRNetworkSize, CIDRNetmask, CIDRNetworkRange

cinp = CInP( 'Utilities', '0.1' )


class UtilitiesException( ValueError ):
  def __init__( self, code, message ):
    super().__init__( message )
    self.message = message
    self.code = code

  @property
  def response_data( self ):
    return { 'class': 'UtilitiesException', 'error': self.code, 'message': self.message }

  def __str__( self ):
    return 'UtilitiesException ({0}): {1}'.format( self.code, self.message )


def ipAddress2Native( ip_address ):
  try:
    address_block = AddressBlock.objects.get( subnet__lte=ip_address, _max_address__gte=ip_address )
  except AddressBlock.DoesNotExist:
    raise UtilitiesException( 'ADDRESS_NOT_FOUND', 'ip_address "{0}" does not exist in any existing Address Blocks'.format( ip_address ) )

  return address_block, StrToIp( ip_address ) - StrToIp( address_block.subnet )


@cinp.model()
class Networked( models.Model ):
  hostname = models.CharField( max_length=100 )
  site = models.ForeignKey( Site, on_delete=models.PROTECT )

  @property
  def subclass( self ):
    try:
      return self.structure
    except AttributeError:
      pass

    return self

  @property
  def primary_interface( self ):
    try:
      address = self.address_set.get( is_primary=True )
      return address.interface
    except ObjectDoesNotExist:
      return None

  @property
  def primary_address( self ):
    try:
      return self.address_set.get( is_primary=True )
    except Address.DoesNotExist:
      return None

  @property
  def provisioning_interface( self ):
    try:
      return self.structure.foundation.networkinterface_set.get( is_provisioning=True )
    except ObjectDoesNotExist:
      return None

  @property
  def provisioning_address( self ):
    provisioning_interface = self.provisioning_interface
    if provisioning_interface is None:
      return None

    interface_name = provisioning_interface.name
    if interface_name is None:
      return None

    try:
      return self.address_set.get( interface_name=interface_name, is_primary=True )
    except Address.DoesNotExist:
      try:
        return self.address_set.filter( interface_name=interface_name )[ 0 ]
      except IndexError:
        pass
      return None

  @property
  def domain_name( self ):
    try:
      zone = self.site.zone
      if zone is None:
        return None

    except ( ObjectDoesNotExist, AttributeError ):
      return None

    return zone.fqdn

  @property
  def fqdn( self ):
    try:
      zone = self.site.zone
      if zone is None:
        return self.hostname

    except ( ObjectDoesNotExist, AttributeError ):
      return self.hostname

    return '{0}.{1}'.format( self.hostname, zone.fqdn )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not hostname_regex.match( self.hostname ):
      errors[ 'hostname' ] = 'Structure hostname "{0}" is invalid'.format( self.hostname )

    try:
      zone = self.site.zone
    except ( ObjectDoesNotExist, AttributeError ):
      zone = None

    if zone is not None and zone.site_set.filter( networked__hostname=self.hostname ).exclude( networked__pk=self.pk ).count():
      errors[ 'hostname' ] = 'Hostname "{0}" allready used in DNS Zone "{1}"'.format( self.hostname, zone.pk )

    if errors:
      raise ValidationError( errors )

  class Meta:
    unique_together = ( ( 'site', 'hostname' ), )

  def __str__( self ):
    return 'Networked hostname "{0}" in "{1}"'.format( self.hostname, self.site.name )


@cinp.model( property_list=( 'gateway', 'netmask', 'size', 'isIpV4' ) )
class AddressBlock( models.Model ):
  name = models.CharField( max_length=40 )
  site = models.ForeignKey( Site, on_delete=models.PROTECT )
  subnet = IpAddressField()
  prefix = models.IntegerField()
  gateway_offset = models.IntegerField( blank=True, null=True )
  _max_address = IpAddressField( editable=False )
  updated = models.DateTimeField( editable=False, auto_now=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )

  @property
  def gateway( self ):
    if self.gateway_offset is None:
      return None

    return IpToStr( StrToIp( self.subnet ) + self.gateway_offset )

  @property
  def netmask( self ):
    return IpToStr( CIDRNetmask( self.prefix, not self.isIpV4 ) )

  @property
  def size( self ):
    return CIDRNetworkSize( StrToIp( self.subnet ), self.prefix, not self.isIpV4 )

  @property
  def offsetBounds( self ):
    return CIDRNetworkBounds( StrToIp( self.subnet ), self.prefix, include_unusable=False, as_offsets=True )

  @property
  def isIpV4( self ):
    return IpIsV4( StrToIp( self.subnet ) )

  @cinp.action( return_type={ 'type': 'Model', 'model': 'contractor.Utilities.models.Address' }, paramater_type_list=[ { 'type': 'Model', 'model': 'contractor.Utilities.models.Networked' }, { 'type': 'String' }, { 'type': 'Boolean' } ] )
  def nextAddress( self, networked, interface_name, is_primary ):  # TODO: wrap this in a transaction, or some other way to unwrap everything if it fails
    address = Address( networked=networked, interface_name=interface_name, is_primary=is_primary )
    if networked.structure.foundation.subclass.__class__.__name__ == 'DockerFoundation':
      # address.pointer = Address.objects.get( networked=structure.foundation.docker_host.members[0], interface_name='eth0' )
      return None  # set map_ports will do the address

    else:  # TODO: either retry till all_offsets is empty, or lock the Address table(s)
      all_offsets = set( CIDRNetworkRange( StrToIp( self.subnet ), self.prefix, False, True ) )
      if self.gateway_offset is not None:
        all_offsets = all_offsets - set( [ self.gateway_offset ] )

      if not all_offsets:
        raise UtilitiesException( 'NO_OFFSETS', 'No Available Offsets' )

      used_offsets = set( BaseAddress.objects.filter( address_block=self, offset__isnull=False ).values_list( 'offset', flat=True ) )
      address.address_block = self
      address.offset = random.choice( list( all_offsets - used_offsets ) )

    address.full_clean()
    address.save()

    return address

  @cinp.action( return_type='Map' )
  def usage( self ):
    result = {}
    result[ 'total' ] = self.size
    result[ 'static' ] = Address.objects.filter( address_block=self ).count()
    result[ 'reserved' ] = ReservedAddress.objects.filter( address_block=self ).count()
    result[ 'dynamic' ] = DynamicAddress.objects.filter( address_block=self ).count()
    if self.gateway_offset:
      result[ 'reserved' ] += 1

    return result

  @cinp.list_filter( name='site', paramater_type_list=[ { 'type': 'Model', 'model': Site } ] )
  @staticmethod
  def filter_site( site ):
    return AddressBlock.objects.filter( site=site )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not name_regex.match( self.name ):
      errors[ 'name' ] = 'invalid'

    try:
      subnet_ip = StrToIp( self.subnet )
      ipv4 = IpIsV4( subnet_ip )
    except ValueError:
      ipv4 = None
      errors[ 'subnet' ] = 'Invalid Ip Address'

    if self.prefix is None or self.prefix < 1:
      errors[ 'prefix' ] = 'Min Prefix is 1'

    if errors:  # no point in continuing
      raise ValidationError( errors )

    if ipv4 is not None:
      if ipv4:
        if self.prefix > 32:
          errors[ 'prefix' ] = 'Max Prefix for ipv4 is 32'
      else:
        if self.prefix > 128:
          errors[ 'prefix' ] = 'Max Prefix for ipv6 is 128'

      if self.gateway_offset is not None:
        ( low, high ) = CIDRNetworkBounds( subnet_ip, self.prefix, False, True )
        if low == high:
          errors[ 'gateway_offset' ] = 'Gateway not possible in single host subnet'

        if self.gateway_offset < low or self.gateway_offset > high:
          errors[ 'gateway_offset' ] = 'Must be greater than {0} and less than {1}'.format( low, high )

    if errors:  # no point in continuing
      raise ValidationError( errors )

    ( subnet_ip, last_ip ) = CIDRNetworkBounds( subnet_ip, self.prefix, True )
    self.subnet = IpToStr( subnet_ip )
    self._max_address = IpToStr( last_ip )

    if self.pk is not None:
      ABobjects = AddressBlock.objects.filter( ~Q( pk=self.pk ), site=self.site )
    else:
      ABobjects = AddressBlock.objects.filter( site=self.site )
    block_count = ABobjects.filter( subnet__gte=self.subnet, _max_address__lte=self.subnet ).count()
    block_count += ABobjects.filter( subnet__gte=self._max_address, _max_address__lte=self._max_address ).count()
    block_count += ABobjects.filter( _max_address__gte=self.subnet, _max_address__lte=self._max_address ).count()
    block_count += ABobjects.filter( subnet__gte=self.subnet, subnet__lte=self._max_address ).count()
    if block_count > 0:
      errors[ 'subnet' ] = 'This subnet/prefix overlaps with an existing Address Block in the same site'

    if errors:
      raise ValidationError( errors )

  class Meta:
    unique_together = ( ( 'site', 'name' ), )

  def __str__( self ):
    return 'AddressBlock "{0}" in "{1}" subnet "{2}/{3}"'.format( self.name, self.site, self.subnet, self.prefix )


@cinp.model()
class Network( models.Model ):
  name = models.CharField( max_length=40 )
  site = models.ForeignKey( Site, on_delete=models.PROTECT )
  address_block_list = models.ManyToManyField( AddressBlock, through='NetworkAddressBlock' )
  updated = models.DateTimeField( editable=False, auto_now=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  class Meta:
    unique_together = ( ( 'site', 'name' ), )

  def __str__( self ):
    return 'Network "{0}" in "{1}"'.format( self.name, self.site )


@cinp.model()
class NetworkAddressBlock( models.Model ):
  network = models.ForeignKey( Network, on_delete=models.CASCADE )
  address_block = models.ForeignKey( AddressBlock, on_delete=models.CASCADE )
  vlan = models.IntegerField( default=0 )  # vlan = 0: Untagged/Native VLAN     vlan = 4096: Trunked
  vlan_tagged = models.BooleanField( default=False )
  updated = models.DateTimeField( editable=False, auto_now=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}

    if self.vlan > 4096 or self.vlan < 0:
      errors[ 'vlan' ] = 'must be between 0 and 4096'

    if self.vlan_tagged and self.vlan == 0:
      errors[ 'vlan_tagged' ] = 'can not tag vlan 0'

    if errors:
      raise ValidationError( errors )

  def __str__( self ):
    return 'NetworkAddressBlock "{0}" to "{1}"'.format( self.network, self.address_block )


@cinp.model( not_allowed_verb_list=[ 'LIST', 'GET', 'CREATE', 'UPDATE', 'DELETE', 'CALL' ] )
class NetworkInterface( models.Model ):
  name = models.CharField( max_length=20 )
  is_provisioning = models.BooleanField( default=False )
  network = models.ForeignKey( Network, on_delete=models.PROTECT )
  updated = models.DateTimeField( editable=False, auto_now=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )

  @property
  def subclass( self ):
    try:
      return self.realnetworkinterface
    except AttributeError:
      pass

    try:
      return self.abstractnetworkinterface
    except AttributeError:
      pass

    try:
      return self.abstractnetworkinterface
    except AttributeError:
      pass

    return self

  @property
  def type( self ):
    return 'Unknown'  # This class should not be used directly

  @property
  def config( self ):
    result = { 'name': self.name, 'network': self.network.name, 'address_list': [] }
    try:
      structure = self.foundation.structure
    except AttributeError:
      structure = None

    if structure is not None:
      for address in structure.address_set.filter( interface_name=self.name ):
        nab = NetworkAddressBlock( network=self.network, address_block=address.address_block )
        address_config = address.as_dict
        address_config[ 'vlan' ] = nab.vlan
        address_config[ 'tagged' ] = nab.vlan_tagged
        result[ 'address_list' ].append( address_config )

    return result

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    if verb == 'DESCRIBE':
      return True

    return False

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not name_regex.match( self.name ):
      errors[ 'name' ] = '"{0}" is invalid'.format( self.name[ 0:50 ] )

    if errors:
      raise ValidationError( errors )

  def __str__( self ):
    return 'NetworkInterface "{0}"'.format( self.name )


@cinp.model( )
class RealNetworkInterface( NetworkInterface ):
  mac = models.CharField( max_length=18, blank=True, null=True )  # in a globally unique world we would set this to unique, but these virtual days we have to many ways to use the same mac safely, so good luck.
  foundation = models.ForeignKey( 'Building.Foundation', related_name='networkinterface_set', on_delete=models.CASCADE )
  physical_location = models.CharField( max_length=100 )
  link_name = models.CharField( max_length=100, blank=True, null=True )  # Until NetworkInterfaces can plug to each other and better ways of storing LLDP info
  pxe = models.ForeignKey( PXE, related_name='+', blank=True, null=True, on_delete=models.PROTECT )

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'Real'

  @property
  def config( self ):
    result = super().config
    result[ 'mac' ] = self.mac
    result[ 'physical_location' ] = self.physical_location

    return result

  @cinp.list_filter( name='foundation', paramater_type_list=[ { 'type': 'Model', 'model': 'contractor.Building.models.Foundation' } ] )
  @staticmethod
  def filter_foundation( foundation ):
    return RealNetworkInterface.objects.filter( foundation=foundation )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not self.mac:
      self.mac = None

    else:
      self.mac = self.mac.lower()

      if re.match( '([0-9a-f]{4}.){2}[0-9a-f]{4}', self.mac ):
        self.mac = self.mac.replace( '.', '' )

      if re.match( '[0-9a-f]{12}', self.mac ):  # this is #2, it will catch the stripped cisco notation, and the  : less notation
        self.mac = ':'.join( [ self.mac[ i: i + 2 ] for i in range( 0, 12, 2 ) ] )

      if not re.match( '([0-9a-f]{2}:){5}[0-9a-f]{2}', self.mac ):
        errors[ 'mac' ] = '"{0}" is invalid'.format( self.mac[ 0:50 ] )

    if errors:
      raise ValidationError( errors )

  class Meta:
    unique_together = ( ( 'foundation', 'physical_location' ), )

  def __str__( self ):
    return 'RealNetworkInterface "{0}" mac "{1}"'.format( self.name, self.mac )


@cinp.model( )
class AbstractNetworkInterface( NetworkInterface ):

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'Abstract'

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def __str__( self ):
    return 'AbstractNetworkInterface "{0}"'.format( self.name )


@cinp.model( )
class AggregatedNetworkInterface( AbstractNetworkInterface ):
  master_interface = models.ForeignKey( NetworkInterface, related_name='+', on_delete=models.CASCADE )
  slaves = models.ManyToManyField( NetworkInterface, related_name='+' )
  paramaters = MapField()

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'Aggragated'

  @property
  def config( self ):
    result = super().config
    result[ 'master' ] = self.master_interface.name
    result[ 'slaves' ] = [ i.name for i in self.slaves ]

    return result

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def __str__( self ):
    return 'AggregatedNetworkInterface "{0}"'.format( self.name )


@cinp.model( not_allowed_verb_list=[ 'LIST', 'GET', 'CREATE', 'UPDATE', 'DELETE' ], property_list=( 'type', 'ip_address', 'subnet', 'netmask', 'prefix', 'gateway' ) )
class BaseAddress( models.Model ):
  address_block = models.ForeignKey( AddressBlock, blank=True, null=True, on_delete=models.CASCADE )
  offset = models.IntegerField( blank=True, null=True )
  updated = models.DateTimeField( editable=False, auto_now=True )
  created = models.DateTimeField( editable=False, auto_now_add=True )

  @property
  def ip_address( self ):
    if self.address_block is None or self.offset is None:
      return None

    return IpToStr( StrToIp( self.address_block.subnet ) + self.offset )

  @property
  def subnet( self ):
    if self.address_block is None:
      return None

    return self.address_block.subnet

  @property
  def netmask( self ):
    if self.address_block is None:
      return None

    return self.address_block.netmask

  @property
  def prefix( self ):
    if self.address_block is None:
      return None

    return self.address_block.prefix

  @property
  def gateway( self ):
    if self.address_block is None:
      return None

    return self.address_block.gateway

  @property
  def as_dict( self ):
    return {
             'address': self.ip_address,  # set to 'dhcp' for dhcp
             'netmask': self.netmask,
             'prefix': self.prefix,
             'subnet': self.subnet,
             'gateway': self.gateway,
             'auto': True,
             'mtu': 1500
           }

  @property
  def interface( self ):
      return None

  @property
  def subclass( self ):
    try:
      return self.address
    except AttributeError:
      pass

    try:
      return self.reservedaddress
    except AttributeError:
      pass

    try:
      return self.dynamicaddress
    except AttributeError:
      pass

    return self

  @property
  def type( self ):
    real = self.subclass
    if real.__class__.__name__ == 'BaseAddress':
      return 'Unknown'

    return real.type

  @cinp.action( return_type={ 'type': 'Model', 'model': 'contractor.Utilities.models.BaseAddress' }, paramater_type_list=[ 'String' ] )
  @staticmethod
  def lookup( ip_address ):
    try:
      ip_address_ip = StrToIp( ip_address )
    except ValueError:
      return None

    ip_address = IpToStr( ip_address_ip )  # so it is in a consistant format
    try:
      address_block = AddressBlock.objects.get( subnet__lte=ip_address, _max_address__gte=ip_address )
    except AddressBlock.DoesNotExist:
      return None

    offset = ip_address_ip - StrToIp( address_block.subnet )
    try:
      return BaseAddress.objects.get( address_block=address_block, offset=offset )
    except BaseAddress.DoesNotExist:
      return None

    return None

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    if verb == 'DESCRIBE':
      return True

    return False

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}

    if self.offset is not None and self.address_block is None:
      errors[ 'offset' ] = 'Can not be specified without address_block'

    if self.address_block is not None and self.offset is None:
      errors[ 'address_block' ] = 'Can not be specified without offset'

    if self.address_block is not None and self.offset is not None:
      ( min_offset, max_offset ) = self.address_block.offsetBounds
      if self.offset is None or self.offset < min_offset or self.offset > max_offset:
        errors[ 'offset' ] = 'Must be greater than {0} and less than {1}'.format( min_offset, max_offset )

      if 'offest' not in errors and self.offset == self.address_block.gateway_offset:
        errors[ 'offset' ] = 'Conflicts with Gateway'

    if errors:
      raise ValidationError( errors )

  class Meta:
    unique_together = ( ( 'address_block', 'offset' ), )

  def __str__( self ):
    return 'BaseAddress block "{0}" offset "{1}"'.format( self.address_block, self.offset )


@cinp.model( property_list=( 'type', 'ip_address', 'subnet', 'netmask', 'prefix', 'gateway' ) )
class Address( BaseAddress ):
  networked = models.ForeignKey( Networked, on_delete=models.CASCADE )
  interface_name = models.CharField( max_length=20 )
  sub_interface = models.IntegerField( default=None, blank=True, null=True )
  pointer = models.ForeignKey( 'self', blank=True, null=True, on_delete=models.PROTECT )
  is_primary = models.BooleanField( default=False )

  @property
  def ip_address( self ):
    if self.pointer is not None:
      return self.pointer.ip_address

    return super().ip_address

  @property
  def subnet( self ):
    if self.pointer is not None:
      return self.pointer.subnet

    return super().subnet

  @property
  def netmask( self ):
    if self.pointer is not None:
      return self.pointer.netmask

    return super().netmask

  @property
  def prefix( self ):
    if self.pointer is not None:
      return self.pointer.prefix

    return super().prefix

  @property
  def gateway( self ):
    if self.pointer is not None:
      return self.pointer.gateway

    return super().gateway

  @property
  def structure( self ):
    try:
      return self.networked.structure
    except ObjectDoesNotExist:
      return None

  @property
  def as_dict( self ):
    result = super().as_dict
    result[ 'sub_interface' ] = self.sub_interface
    result[ 'primary' ] = self.is_primary
    return result

  @property
  def interface( self ):
    try:
      return self.networked.structure.foundation.networkinterface_set.get( name=self.interface_name )
    except ObjectDoesNotExist:
      return None

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'Address'

  @cinp.list_filter( name='address_block', paramater_type_list=[ { 'type': 'Model', 'model': AddressBlock } ] )
  @staticmethod
  def filter_address_block( address_block ):
    return Address.objects.filter( address_block=address_block )

  @cinp.list_filter( name='structure', paramater_type_list=[ { 'type': 'Model', 'model': 'contractor.Building.models.Structure' } ] )
  @staticmethod
  def filter_structure( structure ):
    return Address.objects.filter( networked=structure )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not name_regex.match( self.interface_name ):
      errors[ 'interface_name' ] = '"{0}" is invalid'.format( self.interface_name[ 0:50 ] )

    try:
      if self.address_block and self.networked and self.address_block.site != self.networked.site:
        errors[ 'address_block' ] = 'Address is not in the same site as the Networked it belongs to'
    except ObjectDoesNotExist:
      pass  # something else should make sure address_block and networked are defined

    if errors:  # if either of the above happen, don't bother with the rest
      raise ValidationError( errors )

    try:
      if self.pointer is not None:
        if self.address_block is not None:
          errors[ 'address_block' ] = 'Conflicts with Pointer'
          errors[ 'pointer' ] = 'Conflicts with Address_block'
        if self.offset is not None:
          errors[ 'offset' ] = 'Conflicts with Pointer'
          errors[ 'pointer' ] = 'Conflicts with Offset'
    except ObjectDoesNotExist:
      pass

    if not self.sub_interface:
      self.sub_interface = None
    else:
      if self.sub_interface < 0:
        errors[ 'sub_interface' ] = 'Must be a positive value'

    if self.is_primary:
      if self.pk is not None:
        Aobjects = self.networked.address_set.filter( ~Q( pk=self.pk ) )
      else:
        Aobjects = self.networked.address_set.all()

      if Aobjects.filter( is_primary=True ).count() > 0:
        errors[ 'is_primary' ] = 'Networked allready has a primary ip'

    if errors:
      raise ValidationError( errors )

  def __str__( self ):
    return 'Address in Block "{0}" offset "{1}" networked "{2}" on interface "{3}"'.format( self.address_block, self.offset, self.networked, self.interface_name )


@cinp.model( property_list=( 'type', 'ip_address', 'subnet', 'netmask', 'prefix', 'gateway' ) )
class ReservedAddress( BaseAddress ):
  reason = models.CharField( max_length=50 )

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'ReservedAddress'

  @cinp.list_filter( name='address_block', paramater_type_list=[ { 'type': 'Model', 'model': AddressBlock } ] )
  @staticmethod
  def filter_address_block( address_block ):
    return ReservedAddress.objects.filter( address_block=address_block )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not self.address_block:
      errors[ 'address_block' ] = 'This field cannot be blank.'

    if not self.offset:
      errors[ 'offset' ] = 'This field cannot be blank.'

    if errors:
      raise ValidationError( errors )

  def __str__( self ):
    return 'ReservedAddress block "{0}" offset "{1}"'.format( self.address_block, self.offset )


@cinp.model( property_list=( 'type', 'ip_address', 'subnet', 'netmask', 'prefix', 'gateway' ) )
class DynamicAddress( BaseAddress ):  # no dynamic pools, thoes will be auto detected
  pxe = models.ForeignKey( PXE, related_name='+', blank=True, null=True, on_delete=models.CASCADE )

  @property
  def subclass( self ):
    return self

  @property
  def type( self ):
    return 'DynamicAddress'

  @cinp.list_filter( name='address_block', paramater_type_list=[ { 'type': 'Model', 'model': AddressBlock } ] )
  @staticmethod
  def filter_address_block( address_block ):
    return DynamicAddress.objects.filter( address_block=address_block )

  @cinp.check_auth()
  @staticmethod
  def checkAuth( user, verb, id_list, action=None ):
    return True

  def clean( self, *args, **kwargs ):
    super().clean( *args, **kwargs )
    errors = {}
    if not self.address_block:
      errors[ 'address_block' ] = 'This field cannot be blank.'

    if not self.offset:
      errors[ 'offset' ] = 'This field cannot be blank.'

    if errors:
      raise ValidationError( errors )

  def __str__( self ):
    return 'DynamicAddress block "{0}" offset "{1}"'.format( self.address_block, self.offset )


# and Powered
# class PowerPort( models.Model ):
#   other_end = models.ForeignKey( 'self' , on_delete=models.CASCADE ) # or should there be a sperate table with the plug relation ships
#   updated = models.DateTimeField( editable=False, auto_now=True )
#   created = models.DateTimeField( editable=False, auto_now_add=True )
#   # powered by Structure
#   # provides power to foundation
