# Copyright 2016 Cloudbase Solutions Srl
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from oslo_concurrency import lockutils
from oslo_log import log as logging

from os_brick import exception
from os_brick.initiator.solaris import base as sol_conn_base
from os_brick import utils
from os_brick.initiator.solaris import solarisfc

LOG = logging.getLogger(__name__)

synchronized = lockutils.synchronized_with_prefix('os-brick-')


class SolarisFCConnector(sol_conn_base.BaseSolarisConnector):
    """Connector class to attach/detach Fibre Channel volumes."""

    def __init__(self, root_helper, driver=None,
                 execute=None, use_multipath=False,
                 *args, **kwargs):
        self._solarisfc = solarisfc.SolarisFibreChannel(root_helper, execute)
        super(SolarisFCConnector, self).__init__(
            root_helper, driver=driver,
            execute=execute,
            *args, **kwargs)
        self.use_multipath = use_multipath
        LOG.debug('SolarisFCConnector initialized')

    def set_execute(self, execute):
        super(SolarisFCConnector, self).set_execute(execute)
        self._solarisfc.set_execute(execute)

    @staticmethod
    def get_connector_properties(root_helper, *args, **kwargs):
        """The Fibre Channel connector properties."""
        props = {}
        fc = solarisfc.SolarisFibreChannel(root_helper,
                                       execute=kwargs.get('execute'))

        wwpns = fc.get_fc_wwpns()
        if wwpns:
            props['wwpns'] = wwpns
        wwnns = fc.get_fc_wwnns()
        if wwnns:
            props['wwnns'] = wwnns

        return props

    def get_volume_paths(self, connection_properties):
        LOG.error("Unknown or unimplemented %s", connection_properties)
        volume_paths = []
        # first fetch all of the potential paths that might exist
        # how the FC fabric is zoned may alter the actual list
        # that shows up on the system.  So, we verify each path.
        hbas = self._linuxfc.get_fc_hbas_info()
        device_paths = self._get_possible_volume_paths(
            connection_properties, hbas)
        for path in device_paths:
            if os.path.exists(path):
                volume_paths.append(path)

        return volume_paths

    @utils.trace
    @synchronized('extend_volume')
    def extend_volume(self, connection_properties):
        LOG.error("Unknown or unimplemented %s", connection_properties)
        """Update the local kernel's size information.

        Try and update the local kernel's size information
        for an FC volume.
        """
        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        volume_paths = self.get_volume_paths(connection_properties)
        if volume_paths:
            return self._linuxscsi.extend_volume(volume_paths)
        else:
            LOG.warning("Couldn't find any volume paths on the host to "
                        "extend volume for %(props)s",
                        {'props': connection_properties})
            raise exception.VolumePathsNotFound()

    @utils.trace
    @synchronized('connect_volume')
    def connect_volume(self, connection_properties):
        LOG.error("Unknown or unimplemented %s", connection_properties)
        """Attach the volume to instance_name.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :returns: dict

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """
        LOG.debug("execute = %s", self._execute)
        device_info = {'type': 'block'}

        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        hbas = self._linuxfc.get_fc_hbas_info()
        host_devices = self._get_possible_volume_paths(
            connection_properties, hbas)

        if len(host_devices) == 0:
            # this is empty because we don't have any FC HBAs
            LOG.warning("We are unable to locate any Fibre Channel devices")
            raise exception.NoFibreChannelHostsFound()

        # The /dev/disk/by-path/... node is not always present immediately
        # We only need to find the first device.  Once we see the first device
        # multipath will have any others.
        def _wait_for_device_discovery(host_devices):
            for device in host_devices:
                LOG.debug("Looking for Fibre Channel dev %(device)s",
                          {'device': device})
                if os.path.exists(device) and self.check_valid_device(device):
                    self.host_device = device
                    # get the /dev/sdX device.  This is used
                    # to find the multipath device.
                    self.device_name = os.path.realpath(device)
                    raise loopingcall.LoopingCallDone()

            if self.tries >= self.device_scan_attempts:
                LOG.error("Fibre Channel volume device not found.")
                raise exception.NoFibreChannelVolumeDeviceFound()

            LOG.info("Fibre Channel volume device not yet found. "
                     "Will rescan & retry.  Try number: %(tries)s.",
                     {'tries': self.tries})

            self._solarisfc.rescan_hosts(hbas, connection_properties)
            self.tries = self.tries + 1

        self.host_device = None
        self.device_name = None
        self.tries = 0
        timer = loopingcall.FixedIntervalLoopingCall(
            _wait_for_device_discovery, host_devices)
        timer.start(interval=2).wait()

        if self.host_device is not None and self.device_name is not None:
            LOG.debug("Found Fibre Channel volume %(name)s "
                      "(after %(tries)s rescans)",
                      {'name': self.device_name, 'tries': self.tries})

        # find out the WWN of the device
        device_wwn = self._linuxscsi.get_scsi_wwn(self.host_device)
        LOG.debug("Device WWN = '%(wwn)s'", {'wwn': device_wwn})
        device_info['scsi_wwn'] = device_wwn

        # see if the new drive is part of a multipath
        # device.  If so, we'll use the multipath device.
        if self.use_multipath:
            (device_path, multipath_id) = (super(
                FibreChannelConnector, self)._discover_mpath_device(
                device_wwn, connection_properties, self.device_name))
            if multipath_id:
                # only set the multipath_id if we found one
                device_info['multipath_id'] = multipath_id

        else:
            device_path = self.host_device

        device_info['path'] = device_path
        LOG.debug("connect_volume returning %s", device_info)
        return device_info

    @utils.trace
    @synchronized('connect_volume')
    def disconnect_volume(self, connection_properties, device_info,
                          force=False, ignore_errors=False):
        LOG.error("Unknown or unimplemented %s, %s, %s, %s", connection_properties, device_info, force, ignore_errors)
        """Detach the volume from instance_name.

        :param connection_properties: The dictionary that describes all
                                      of the target volume attributes.
        :type connection_properties: dict
        :param device_info: historical difference, but same as connection_props
        :type device_info: dict

        connection_properties for Fibre Channel must include:
        target_wwn - World Wide Name
        target_lun - LUN id of the volume
        """

        devices = []
        wwn = None

        connection_properties = self._add_targets_to_connection_properties(
            connection_properties)

        volume_paths = self.get_volume_paths(connection_properties)
        mpath_path = None
        for path in volume_paths:
            real_path = self._linuxscsi.get_name_from_path(path)
            if self.use_multipath and not mpath_path:
                wwn = self._linuxscsi.get_scsi_wwn(path)
                mpath_path = self._linuxscsi.find_multipath_device_path(wwn)
                if mpath_path:
                    self._linuxscsi.flush_multipath_device(mpath_path)
            device_info = self._linuxscsi.get_device_info(real_path)
            devices.append(device_info)

        LOG.debug("devices to remove = %s", devices)
        self._remove_devices(connection_properties, devices, device_info)

    def _remove_devices(self, connection_properties, devices, device_info):
        LOG.error("Unknown or unimplemented %s, %s, %s", connection_properties, devices, device_info)
        # There may have been more than 1 device mounted
        # by the kernel for this volume.  We have to remove
        # all of them
        path_used = self._linuxscsi.get_dev_path(connection_properties,
                                                 device_info)
        was_multipath = '/pci-' not in path_used
        for device in devices:
            device_path = device['device']
            flush = self._linuxscsi.requires_flush(device_path,
                                                   path_used,
                                                   was_multipath)
            self._linuxscsi.remove_scsi_device(device_path, flush=flush)
