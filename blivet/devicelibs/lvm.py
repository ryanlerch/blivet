#
# lvm.py
# lvm functions
#
# Copyright (C) 2009-2014  Red Hat, Inc.  All rights reserved.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Author(s): Dave Lehman <dlehman@redhat.com>
#

import math
from decimal import Decimal

import logging
log = logging.getLogger("blivet")

from ..size import Size
from .. import util
from .. import arch
from ..errors import LVMError
from ..i18n import _

MAX_LV_SLOTS = 256

# some of lvm's defaults that we have no way to ask it for
LVM_PE_START = Size("1 MiB")
LVM_PE_SIZE = Size("4 MiB")

# thinp constants
LVM_THINP_MIN_METADATA_SIZE = Size("2 MiB")
LVM_THINP_MAX_METADATA_SIZE = Size("16 GiB")
LVM_THINP_MIN_CHUNK_SIZE = Size("64 KiB")
LVM_THINP_MAX_CHUNK_SIZE = Size("1 GiB")

def has_lvm():
    if util.find_program_in_path("lvm"):
        for line in open("/proc/devices").readlines():
            if "device-mapper" in line.split():
                return True

    return False

# Start config_args handling code
#
# Theoretically we can handle all that can be handled with the LVM --config
# argument.  For every time we call an lvm_cc (lvm compose config) funciton
# we regenerate the config_args with all global info.
config_args_data = { "filterRejects": [],    # regular expressions to reject.
                     "filterAccepts": [] }   # regexp to accept

def _getConfigArgs(**kwargs):
    """lvm command accepts lvm.conf type arguments preceded by --config. """
    config_args = []

    read_only_locking = kwargs.get("read_only_locking", False)

    filter_string = ""
    rejects = config_args_data["filterRejects"]
    for reject in rejects:
        filter_string += ("\"r|/%s$|\"," % reject)

    if filter_string:
        filter_string = "filter=[%s]" % filter_string.strip(",")

    # XXX consider making /tmp/blivet.lvm.XXXXX, writing an lvm.conf there, and
    #     setting LVM_SYSTEM_DIR
    devices_string = 'preferred_names=["^/dev/mapper/", "^/dev/md/", "^/dev/sd"]'
    if filter_string:
        devices_string += " %s" % filter_string

    # devices_string can have (inside the brackets) "dir", "scan",
    # "preferred_names", "filter", "cache_dir", "write_cache_state",
    # "types", "sysfs_scan", "md_component_detection".  see man lvm.conf.
    config_string = " devices { %s } " % (devices_string) # strings can be added
    if filter_string:
        config_string += devices_string # more strings can be added.
    if read_only_locking:
        config_string += "global {locking_type=4} "
    if config_string:
        config_args = ["--config", config_string]
    return config_args

def lvm_cc_addFilterRejectRegexp(regexp):
    """ Add a regular expression to the --config string."""
    log.debug("lvm filter: adding %s to the reject list", regexp)
    config_args_data["filterRejects"].append(regexp)

def lvm_cc_removeFilterRejectRegexp(regexp):
    """ Remove a regular expression from the --config string."""
    log.debug("lvm filter: removing %s from the reject list", regexp)
    try:
        config_args_data["filterRejects"].remove(regexp)
    except ValueError:
        log.debug("%s wasn't in the reject list", regexp)
        return

def lvm_cc_resetFilter():
    config_args_data["filterRejects"] = []
    config_args_data["filterAccepts"] = []
# End config_args handling code.

def getPossiblePhysicalExtents():
    """ Returns a list of possible values for physical extent of a volume group.

        :returns: list of possible extent sizes (:class:`~.size.Size`)
        :rtype: list
    """

    possiblePE = []
    curpe = Size("1 KiB")
    while curpe <= Size("16 GiB"):
        possiblePE.append(curpe)
        curpe = curpe * 2

    return possiblePE

def getMaxLVSize():
    """ Return the maximum size of a logical volume. """
    if arch.getArch() in ("x86_64", "ppc64", "alpha", "ia64", "s390"): #64bit architectures
        return Size("8 EiB")
    else:
        return Size("16 TiB")

def clampSize(size, pesize, roundup=None):
    delta = size % pesize
    if not delta:
        return size

    if roundup:
        clamped = size + (pesize - delta)
    else:
        clamped = size - delta

    return clamped

def get_pv_space(size, disks, pesize=LVM_PE_SIZE):
    """ Given specs for an LV, return total PV space required. """
    # XXX default extent size should be something we can ask of lvm
    # TODO: handle striped and mirrored
    # this is adding one extent for the lv's metadata
    # pylint: disable=unused-argument
    if size == 0:
        return size

    space = clampSize(size, pesize, roundup=True) + pesize
    return space

def get_pool_padding(size, pesize=LVM_PE_SIZE, reverse=False):
    """ Return the size of the pad required for a pool with the given specs.

        reverse means the pad is already included in the specified size and we
        should calculate how much of the total is the pad
    """
    if not reverse:
        multiplier = Decimal('0.2')
    else:
        multiplier = Decimal('1.0') / Decimal('6')

    pad = min(clampSize(size * multiplier, pesize, roundup=True),
              clampSize(LVM_THINP_MAX_METADATA_SIZE, pesize, roundup=True))

    return pad

def is_valid_thin_pool_metadata_size(size):
    """ Return True if size is a valid thin pool metadata vol size.

        :param size: metadata vol size to validate
        :type size: :class:`~.size.Size`
        :returns: whether the size is valid
        :rtype: bool
    """
    return (LVM_THINP_MIN_METADATA_SIZE <= size <= LVM_THINP_MAX_METADATA_SIZE)

# To support discard, chunk size must be a power of two. Otherwise it must be a
# multiple of 64 KiB.
def is_valid_thin_pool_chunk_size(size, discard=False):
    """ Return True if size is a valid thin pool chunk size.

        :param size: chunk size to validate
        :type size: :class:`~.size.Size`
        :keyword discard: whether discard support is required (default: False)
        :type discard: bool
        :returns: whether the size is valid
        :rtype: bool
    """
    if not LVM_THINP_MIN_CHUNK_SIZE <= size <= LVM_THINP_MAX_CHUNK_SIZE:
        return False

    if discard:
        return (math.log(size, 2) % 1.0 == 0)
    else:
        return (size % LVM_THINP_MIN_CHUNK_SIZE == 0)

def lvm(args):
    ret = util.run_program(["lvm"] + args)
    if ret:
        raise LVMError("running lvm " + " ".join(args) + " failed")

def pvcreate(device):
    # we force dataalignment=1024k since we cannot get lvm to tell us what
    # the pe_start will be in advance
    args = ["pvcreate"] + \
            _getConfigArgs() + \
            ["--dataalignment", "1024k"] + \
            [device]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("pvcreate failed for %s: %s" % (device, msg))

def pvresize(device, size):
    args = ["pvresize"] + \
            ["--setphysicalvolumesize", ("%dm" % size.convertTo(spec="mib"))] + \
            _getConfigArgs() + \
            [device]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("pvresize failed for %s: %s" % (device, msg))

def pvremove(device):
    args = ["pvremove", "--force", "--force", "--yes"] + \
            _getConfigArgs() + \
            [device]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("pvremove failed for %s: %s" % (device, msg))

def pvscan(device):
    args = ["pvscan", "--cache",] + \
            _getConfigArgs() + \
            [device]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("pvscan failed for %s: %s" % (device, msg))

def pvmove(source, dest=None):
    """ Move physical extents from one PV to another.

        :param str source: pv device path to move extents off of
        :keyword str dest: pv device path to move the extents onto
    """
    args = ["pvmove"] + _getConfigArgs() + [source]
    if dest:
        args.extend(dest)

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("pvmove failed for %s->%s: %s" % (source, dest, msg))

def parse_lvm_vars(line):
    info = {}
    for var in line.split():
        (name, equals, value) = var.partition("=")
        if not equals:
            continue

        if "," in value:
            val = value.strip().split(",")
        else:
            val = value.strip()

        info[name] = val

    return info

def pvinfo(device=None):
    """ Return a dict with information about LVM PVs.

        :keyword str device: path to PV device node (optional)
        :returns: dict containing PV path keys and udev info dict values
        :rtype: dict

        If device is None we let LVM report on all known PVs.

        If the PV was created with '--metadacopies 0', lvm will do some
        scanning of devices to determine from their metadata which VG
        this PV belongs to.

        pvs -o pv_name,pv_mda_count,vg_name,vg_uuid --config \
            'devices { scan = "/dev" filter = ["a/loop0/", "r/.*/"] }'
    """
    args = ["pvs",
            "--unit=k", "--nosuffix", "--nameprefixes",
            "--unquoted", "--noheadings",
            "-opv_name,pv_uuid,pe_start,vg_name,vg_uuid,vg_size,vg_free,"
            "vg_extent_size,vg_extent_count,vg_free_count,pv_count"] + \
            _getConfigArgs(read_only_locking=True)

    if device:
        args.append(device)

    buf = util.capture_output(["lvm"] + args)
    pvs = {}
    for line in buf.splitlines():
        info = parse_lvm_vars(line)
        if len(info.keys()) != 11:
            log.warning("ignoring pvs output line: %s", line)
            continue

        pvs[info["LVM2_PV_NAME"]] = info

    return pvs

def vgcreate(vg_name, pv_list, pe_size):
    argv = ["vgcreate"]
    if pe_size:
        argv.extend(["-s", "%dm" % pe_size.convertTo(spec="mib")])
    argv.extend(_getConfigArgs())
    argv.append(vg_name)
    argv.extend(pv_list)

    try:
        lvm(argv)
    except LVMError as msg:
        raise LVMError("vgcreate failed for %s: %s" % (vg_name, msg))

def vgremove(vg_name):
    args = ["vgremove", "--force"] + \
            _getConfigArgs() +\
            [vg_name]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("vgremove failed for %s: %s" % (vg_name, msg))

def vgactivate(vg_name):
    args = ["vgchange", "-a", "y"] + \
            _getConfigArgs() + \
            [vg_name]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("vgactivate failed for %s: %s" % (vg_name, msg))

def vgdeactivate(vg_name):
    args = ["vgchange", "-a", "n"] + \
            _getConfigArgs() + \
            [vg_name]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("vgdeactivate failed for %s: %s" % (vg_name, msg))

def vgreduce(vg_name, pv, missing=False):
    """ Remove PVs from a VG.

        :param str pv: PV device path to remove
        :keyword bool missing: whether to remove missing PVs

        When missing is True any specified PV is ignored and vgreduce is
        instead called with the --removemissing option.

        .. note::

            This function does not move extents off of the PV before removing
            it from the VG. You must do that first by calling :func:`.pvmove`.
    """
    args = ["vgreduce"]
    args.extend(_getConfigArgs())
    if missing:
        args.extend(["--removemissing", "--force", vg_name])
    else:
        args.extend([vg_name, pv])

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("vgreduce failed for %s: %s" % (vg_name, msg))

def vgextend(vg_name, pv):
    """ Add a PV to a VG.

        :param str vg_name: the name of the VG
        :param str pv: device path of PV to add
    """
    args = ["vgextend"] + _getConfigArgs() + [vg_name, pv]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("vgextend failed for %s: %s" % (vg_name, msg))

def vginfo(vg_name):
    """ Return a dict with information about an LVM VG.

        :returns: a udev info dict
        :rtype: dict
    """
    args = ["vgs", "--noheadings", "--nosuffix", "--nameprefixes", "--unquoted",
            "--units", "m",
            "-o", "uuid,size,free,extent_size,extent_count,free_count,pv_count"] + \
            _getConfigArgs(read_only_locking=True) + [vg_name]

    buf = util.capture_output(["lvm"] + args)
    info = parse_lvm_vars(buf)
    if len(info.keys()) != 7:
        raise LVMError(_("vginfo failed for %s") % vg_name)

    return info

def lvs(vg_name=None):
    """ Return a dict with information about LVM LVs.

        :keyword str vgname: name of VG to list LVs from (optional)
        :returns: a dict with LV name keys and udev info dict values
        :rtype: dict

        If vg_name is None we let LVM report on all known LVs.
    """
    args = ["lvs",
            "-a", "--unit", "k", "--nosuffix", "--nameprefixes",
            "--unquoted", "--noheadings",
            "-ovg_name,lv_name,lv_uuid,lv_size,lv_attr,segtype"] + \
            _getConfigArgs(read_only_locking=True)
    if vg_name:
        args.append(vg_name)

    buf = util.capture_output(["lvm"] + args)
    logvols = {}
    for line in buf.splitlines():
        info = parse_lvm_vars(line)
        if len(info.keys()) != 6:
            log.debug("ignoring lvs output line: %s", line)
            continue

        lv_name = "%s-%s" % (info["LVM2_VG_NAME"], info["LVM2_LV_NAME"])
        logvols[lv_name] = info

    return logvols

def lvorigin(vg_name, lv_name):
    args = ["lvs", "--noheadings", "-o", "origin"] + \
            _getConfigArgs(read_only_locking=True) + \
            ["%s/%s" % (vg_name, lv_name)]

    buf = util.capture_output(["lvm"] + args)

    try:
        origin = buf.splitlines()[0].strip()
    except IndexError:
        origin = ''

    return origin

def lvcreate(vg_name, lv_name, size, pvs=None):
    pvs = pvs or []

    args = ["lvcreate"] + \
            ["-L", "%dm" % size.convertTo(spec="mib")] + \
            ["-n", lv_name] + \
            ["-y"] + \
            _getConfigArgs() + \
            [vg_name] + pvs

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvcreate failed for %s/%s: %s" % (vg_name, lv_name, msg))

def lvremove(vg_name, lv_name):
    args = ["lvremove"] + \
            _getConfigArgs() + \
            ["%s/%s" % (vg_name, lv_name)]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvremove failed for %s: %s" % (lv_name, msg))

def lvresize(vg_name, lv_name, size):
    args = ["lvresize"] + \
            ["--force", "-L", "%dm" % size.convertTo(spec="mib")] + \
            _getConfigArgs() + \
            ["%s/%s" % (vg_name, lv_name)]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvresize failed for %s: %s" % (lv_name, msg))

def lvactivate(vg_name, lv_name):
    # see if lvchange accepts paths of the form 'mapper/$vg-$lv'
    args = ["lvchange", "-a", "y"] + \
            _getConfigArgs() + \
            ["%s/%s" % (vg_name, lv_name)]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvactivate failed for %s: %s" % (lv_name, msg))

def lvdeactivate(vg_name, lv_name):
    args = ["lvchange", "-a", "n"] + \
            _getConfigArgs() + \
            ["%s/%s" % (vg_name, lv_name)]

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvdeactivate failed for %s: %s" % (lv_name, msg))

def thinpoolcreate(vg_name, lv_name, size, metadatasize=None, chunksize=None):
    args = ["lvcreate", "--thinpool", "%s/%s" % (vg_name, lv_name),
            "--size", "%dm" % size.convertTo(spec="mib")]

    if metadatasize:
        # default unit is MiB
        args += ["--poolmetadatasize", "%d" % metadatasize.convertTo(spec="mib")]

    if chunksize:
        # default unit is KiB
        args += ["--chunksize", "%d" % chunksize.convertTo(spec="kib")]

    args += _getConfigArgs()

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvcreate failed for %s/%s: %s" % (vg_name, lv_name, msg))

def thinlvcreate(vg_name, pool_name, lv_name, size):
    args = ["lvcreate", "--thinpool", "%s/%s" % (vg_name, pool_name),
            "--virtualsize", "%dm" % size.convertTo(spec="MiB"),
            "-n", lv_name] + \
            _getConfigArgs()

    try:
        lvm(args)
    except LVMError as msg:
        raise LVMError("lvcreate failed for %s/%s: %s" % (vg_name, lv_name, msg))

def thinlvpoolname(vg_name, lv_name):
    args = ["lvs", "--noheadings", "-o", "pool_lv"] + \
            _getConfigArgs(read_only_locking=True) + \
            ["%s/%s" % (vg_name, lv_name)]

    buf = util.capture_output(["lvm"] + args)

    try:
        pool = buf.splitlines()[0].strip()
    except IndexError:
        pool = ''

    return pool
