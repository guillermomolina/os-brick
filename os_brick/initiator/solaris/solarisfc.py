# vim: tabstop=4 shiftwidth=4 softtabstop=4
# Copyright (c) 2012 OpenStack LLC.
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

"""Solaris Fibre Channel utilities."""

import errno
import os

from oslo_concurrency import processutils as putils
from oslo_log import log as logging

from os_brick import executor

LOG = logging.getLogger(__name__)


class SolarisFibreChannel(executor.Executor):
    def _get_hba_channel_scsi_target(self, hba, conn_props):
        """Try to get the HBA channel and SCSI target for an HBA.

        This method only works for Fibre Channel targets that implement a
        single WWNN for all ports, so caller should expect us to return either
        explicit channel and targets or wild cards if we cannot determine them.

        The connection properties will need to have "target" values defined in
        it which are expected to be tuples of (wwpn, lun).

        :returns: List of lists with [c, t, l] entries, the channel and target
        may be '-' wildcards if unable to determine them.
        """
        # We want the target's WWPNs, so we use the initiator_target_map if
        # present for this hba or default to target_wwns if not present.
        targets = conn_props['targets']
        if 'initiator_target_map' in conn_props:
            targets = conn_props['initiator_target_lun_map'].get(
                hba['HBAPortWWN'], targets)

        # Leave only the number from the host_device field (ie: host6)
        host_device = hba['OSDeviceName']

        path = host_device
        ctls = []
        for wwpn, lun in targets:
            cmd = 'grep -Gil "%(wwpns)s" %(path)s*/port_name' % {'wwpns': wwpn,
                                                                 'path': path}
            try:
                # We need to run command in shell to expand the * glob
                out, _err = self._execute(cmd, shell=True)
                ctls += [line.split('/')[4].split(':')[1:] + [lun]
                         for line in out.split('\n') if line.startswith(path)]
            except Exception as exc:
                LOG.debug('Could not get HBA channel and SCSI target ID, path:'
                          ' %(path)s*, reason: %(reason)s', {'path': path,
                                                             'reason': exc})
                # If we didn't find any paths just give back wildcards for
                # the channel and target ids.
                ctls.append(['-', '-', lun])
        return ctls

    def rescan_hosts(self, hbas, connection_properties):
        LOG.debug('Rescaning HBAs %(hbas)s with connection properties '
                  '%(conn_props)s', {'hbas': hbas,
                                     'conn_props': connection_properties})
        get_ctsl = self._get_hba_channel_scsi_target

        # Use initiator_target_map provided by backend as HBA exclusion map
        ports = connection_properties.get('initiator_target_lun_map')
        if ports:
            hbas = [hba for hba in hbas if hba['HBAPortWWN'] in ports]
            LOG.debug('Using initiator target map to exclude HBAs')
            process = [(hba, get_ctsl(hba, connection_properties))
                       for hba in hbas]

        # With no target map we'll check if target implements single WWNN for
        # all ports, if it does we only use HBAs connected (info was found),
        # otherwise we are forced to blindly scan all HBAs.
        else:
            with_info = []
            no_info = []

            for hba in hbas:
                ctls = get_ctsl(hba, connection_properties)
                found_info = True
                for hba_channel, target_id, target_lun in ctls:
                    if hba_channel == '-' or target_id == '-':
                        found_info = False
                target_list = with_info if found_info else no_info
                target_list.append((hba, ctls))

            process = with_info or no_info
            msg = "implements" if with_info else "doesn't implement"
            LOG.debug('FC target %s single WWNN for all ports.', msg)

        # luxadm -e forcelip /dev/cfg/c5
        for hba, ctls in process:
            for hba_channel, target_id, target_lun in ctls:
                LOG.debug('Scanning host %(host)s (wwnn: %(wwnn)s, c: '
                          '%(channel)s, t: %(target)s, l: %(lun)s)',
                          {'host': hba['OSDeviceName'],
                           'wwnn': hba['NodeWWN'], 'channel': hba_channel,
                           'target': target_id, 'lun': target_lun})
                self.echo_scsi_command(
                    hba['OSDeviceName'],
                    "%(c)s %(t)s %(l)s" % {'c': hba_channel,
                                           't': target_id,
                                           'l': target_lun})

    def get_fc_hbas(self):
        """Get the Fibre Channel HBA information."""

        out = None
        try:
            out, _err = self._execute('/usr/sbin/fcinfo', 'hba-port',
                                      run_as_root=False,
                                      root_helper=self._root_helper)
        except putils.ProcessExecutionError as exc:
            # This handles the case where rootwrap is used
            # and systool is not installed
            # 96 = nova.cmd.rootwrap.RC_NOEXECFOUND:
            if exc.exit_code == 96:
                LOG.warning("fcinfo is not installed")
            return []
        except OSError as exc:
            # This handles the case where rootwrap is NOT used
            # and systool is not installed
            if exc.errno == errno.ENOENT:
                LOG.warning("fcinfo is not installed")
            return []

        # No FC HBAs were found
        if out is None:
            return []

        lines = out.split('\n')
        hbas = []
        hba = {}
        for line in lines:
            line = line.strip()
            if line.startswith("HBA Port WWN:") and len(hba) > 0:
                hbas.append(hba)
                hba = {}
            val = line.split(':')
            if len(val) == 2:
                key = val[0].strip().replace(" ", "")
                value = val[1].strip()
                hba[key] = value
        if len(hba) > 0:
            hbas.append(hba)

        return hbas

    def get_fc_hbas_info(self):
        """Get Fibre Channel WWNs and device paths from the system, if any."""

        # Note(walter-boring) modern Linux kernels contain the FC HBA's in /sys
        # and are obtainable via the systool app
        hbas = self.get_fc_hbas()

        hbas_info = []
        for hba in hbas:
            wwpn = hba['HBAPortWWN']
            wwnn = hba['NodeWWN']
            device_path = hba['OSDeviceName']
            hbas_info.append({'port_name': wwpn,
                              'node_name': wwnn,
                              'host_device': device_path,
                              'device_path': device_path})
        return hbas_info

    def get_fc_wwpns(self):
        """Get Fibre Channel WWPNs from the system, if any."""

        # Note(walter-boring) modern Linux kernels contain the FC HBA's in /sys
        # and are obtainable via the systool app
        hbas = self.get_fc_hbas()

        wwpns = []
        for hba in hbas:
            if hba['State'] == 'online':
                wwpn = hba['HBAPortWWN']
                wwpns.append(wwpn)

        return wwpns

    def get_fc_wwnns(self):
        """Get Fibre Channel WWNNs from the system, if any."""

        # Note(walter-boring) modern Linux kernels contain the FC HBA's in /sys
        # and are obtainable via the systool app
        hbas = self.get_fc_hbas()

        wwnns = []
        for hba in hbas:
            if hba['State'] == 'online':
                wwnn = hba['NodeWWN']
                wwnns.append(wwnn)

        return wwnns
