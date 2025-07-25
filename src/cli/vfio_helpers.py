#!/usr/bin/env python3
"""VFIO helper functions implementing the complete VFIO workflow."""

import ctypes
import errno
import fcntl
import logging
import os

from .vfio_constants import (VFIO_CHECK_EXTENSION, VFIO_GET_API_VERSION,
                             VFIO_GROUP_FLAGS_VIABLE, VFIO_GROUP_GET_DEVICE_FD,
                             VFIO_GROUP_GET_STATUS, VFIO_GROUP_SET_CONTAINER,
                             VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU,
                             vfio_group_status)

# Import safe logging functions
try:
    from ..string_utils import (log_debug_safe, log_error_safe, log_info_safe,
                                log_warning_safe)
except ImportError:
    # Fallback implementations
    def log_info_safe(logger, template, **kwargs):
        logger.info(template.format(**kwargs))

    def log_error_safe(logger, template, **kwargs):
        logger.error(template.format(**kwargs))

    def log_warning_safe(logger, template, **kwargs):
        logger.warning(template.format(**kwargs))

    def log_debug_safe(logger, template, **kwargs):
        logger.debug(template.format(**kwargs))


# Setup logging
logger = logging.getLogger(__name__)


def check_vfio_prerequisites() -> None:
    """Check VFIO prerequisites before attempting device operations.

    Raises:
        OSError: If VFIO prerequisites are not met
    """
    log_debug_safe(logger, "Checking VFIO prerequisites", prefix="VFIO")

    # Check if VFIO container device exists
    if not os.path.exists("/dev/vfio/vfio"):
        raise OSError(
            "VFIO container device /dev/vfio/vfio not found. "
            "Ensure VFIO kernel module is loaded (modprobe vfio-pci)"
        )

    # Check if we can access the VFIO container
    try:
        # Use os.open() for character devices instead of open() to avoid seekability issues
        test_fd = os.open("/dev/vfio/vfio", os.O_RDWR)
        os.close(test_fd)
    except PermissionError:
        raise OSError(
            "Permission denied accessing /dev/vfio/vfio. "
            "Run as root or ensure proper VFIO permissions are set."
        )
    except OSError as e:
        raise OSError(f"Failed to access VFIO container: {e}")

    # Check if vfio-pci driver is available
    vfio_pci_path = "/sys/bus/pci/drivers/vfio-pci"
    if not os.path.exists(vfio_pci_path):
        raise OSError(
            "vfio-pci driver not found. "
            "Ensure vfio-pci kernel module is loaded (modprobe vfio-pci)"
        )

    log_debug_safe(logger, "VFIO prerequisites check passed", prefix="VFIO")


def check_iommu_group_binding(group: str) -> None:
    """Check if all devices in an IOMMU group are bound to vfio-pci.

    Args:
        group: IOMMU group number

    Raises:
        OSError: If not all devices in the group are bound to vfio-pci
    """
    log_debug_safe(
        logger,
        "Checking IOMMU group {group} device bindings",
        group=group,
        prefix="VFIO",
    )

    group_devices_path = f"/sys/kernel/iommu_groups/{group}/devices"
    if not os.path.exists(group_devices_path):
        raise OSError(
            f"IOMMU group {group} devices path not found: {group_devices_path}"
        )

    try:
        devices = os.listdir(group_devices_path)
        log_debug_safe(
            logger,
            "Devices in IOMMU group {group}: {devices}",
            group=group,
            devices=devices,
            prefix="VFIO",
        )

        unbound_devices = []
        wrong_driver_devices = []

        for device in devices:
            driver_path = f"/sys/bus/pci/devices/{device}/driver"
            if os.path.exists(driver_path):
                try:
                    current_driver = os.path.basename(os.readlink(driver_path))
                    if current_driver != "vfio-pci":
                        wrong_driver_devices.append((device, current_driver))
                except OSError:
                    unbound_devices.append(device)
            else:
                unbound_devices.append(device)

        if unbound_devices or wrong_driver_devices:
            error_msg = f"IOMMU group {group} has devices not bound to vfio-pci:\n"
            if unbound_devices:
                error_msg += f"  Unbound devices: {unbound_devices}\n"
            if wrong_driver_devices:
                error_msg += f"  Wrong driver devices: {wrong_driver_devices}\n"
            error_msg += "All devices in an IOMMU group must be bound to vfio-pci for VFIO to work."
            raise OSError(error_msg)

        log_debug_safe(
            logger,
            "All devices in IOMMU group {group} are properly bound to vfio-pci",
            group=group,
            prefix="VFIO",
        )

    except OSError as e:
        if "not bound to vfio-pci" in str(e):
            raise
        else:
            raise OSError(f"Failed to check IOMMU group {group} bindings: {e}")


def get_device_fd(bdf: str) -> tuple[int, int]:
    """Return an open *device* fd and *container* fd ready for VFIO_DEVICE_* ioctls.

    This implements the complete VFIO workflow as described in the kernel docs:
    1. Check VFIO prerequisites
    2. Find group number from sysfs
    3. Open group fd from /dev/vfio/<group>
    4. Create a container and link the group into it
    5. Ask the group for a device fd
    6. Close group fd (device fd keeps container reference)

    IMPORTANT: The container fd MUST be kept open for as long as you need
    the device fd. Closing the container fd early will make later ioctls fail.

    Args:
        bdf: PCI Bus:Device.Function identifier (e.g., "0000:01:00.0")

    Returns:
        Tuple of (device_fd, container_fd) ready for device-level VFIO operations

    Raises:
        OSError: If any step of the VFIO workflow fails
    """
    log_info_safe(logger, "Starting VFIO device fd acquisition for {bdf}", bdf=bdf)

    # Check VFIO prerequisites first
    check_vfio_prerequisites()

    # 1. Find group number
    sysfs_path = f"/sys/bus/pci/devices/{bdf}/iommu_group"
    log_debug_safe(
        logger,
        "Looking up IOMMU group via {sysfs_path}",
        sysfs_path=sysfs_path,
        prefix="VFIO",
    )

    if not os.path.exists(sysfs_path):
        raise OSError(f"Device {bdf} has no IOMMU group (path not found: {sysfs_path})")

    try:
        group = os.path.basename(os.readlink(sysfs_path))
        log_info_safe(
            logger,
            "Device {bdf} is in IOMMU group {group}",
            bdf=bdf,
            group=group,
            prefix="VFIO",
        )

        # Check that all devices in the IOMMU group are bound to vfio-pci
        check_iommu_group_binding(group)

    except OSError as e:
        raise OSError(f"Failed to read IOMMU group for {bdf}: {e}") from e

    # 2. Open group fd
    grp_path = f"/dev/vfio/{group}"
    log_debug_safe(
        logger, "Opening VFIO group file: {grp_path}", grp_path=grp_path, prefix="VFIO"
    )

    if not os.path.exists(grp_path):
        raise OSError(f"VFIO group file not found: {grp_path}")

    try:
        grp_fd = os.open(grp_path, os.O_RDWR)
        log_debug_safe(
            logger, "Opened group fd: {grp_fd}", grp_fd=grp_fd, prefix="VFIO"
        )
    except OSError as e:
        log_error_safe(
            logger,
            "Failed to open {grp_path}: {error}",
            grp_path=grp_path,
            error=str(e),
        )
        if e.errno == errno.EACCES:
            log_error_safe(
                logger,
                "Permission denied - ensure proper VFIO permissions or run as root",
                prefix="VFIO",
            )
        elif e.errno == errno.ENOENT:
            log_error_safe(
                logger, "Group file not found - check VFIO configuration", prefix="VFIO"
            )
        elif e.errno == errno.EBUSY:
            log_error_safe(
                logger,
                "Group file busy - another process may be using this VFIO group",
                prefix="VFIO",
            )
        raise

    try:
        # 3. Create a container and link the group into it
        log_debug_safe(logger, "Creating VFIO container", prefix="VFIO")
        try:
            cont_fd = os.open("/dev/vfio/vfio", os.O_RDWR)
            log_debug_safe(
                logger, "Opened container fd: {cont_fd}", cont_fd=cont_fd, prefix="VFIO"
            )
        except OSError as e:
            log_error_safe(logger, "Failed to open VFIO container: {e}", e=str(e))
            if e.errno == errno.ENOENT:
                log_error_safe(
                    logger,
                    "VFIO container device not found - ensure VFIO kernel module is loaded",
                    prefix="VFIO",
                )
            elif e.errno == errno.EACCES:
                log_error_safe(
                    logger,
                    "Permission denied accessing VFIO container - run as root or check permissions",
                    prefix="VFIO",
                )
            raise

        try:
            # Check API version
            try:
                api_version = fcntl.ioctl(cont_fd, VFIO_GET_API_VERSION)
                log_debug_safe(
                    logger,
                    "VFIO API version: {api_version}",
                    api_version=api_version,
                    prefix="VFIO",
                )
            except OSError as e:
                log_error_safe(
                    logger,
                    "Failed to get VFIO API version: {e}",
                    e=str(e),
                    prefix="VFIO",
                )
                raise OSError(f"VFIO API version check failed: {e}")

            # Optional: Check if Type1 IOMMU is supported
            try:
                fcntl.ioctl(cont_fd, VFIO_CHECK_EXTENSION, VFIO_TYPE1_IOMMU)
                log_debug_safe(logger, "Type1 IOMMU extension supported", prefix="VFIO")
            except OSError as e:
                log_error_safe(
                    logger,
                    "Type1 IOMMU extension not supported: {e}",
                    e=str(e),
                    prefix="VFIO",
                )
                raise OSError(f"Type1 IOMMU extension required but not supported: {e}")

            try:
                fcntl.ioctl(grp_fd, VFIO_GROUP_SET_CONTAINER, ctypes.c_int(cont_fd))
                log_debug_safe(
                    logger, "Successfully linked group to container", prefix="VFIO"
                )
            except OSError as e:
                log_error_safe(
                    logger,
                    "Failed to link group {group} to container: {e}",
                    group=group,
                    e=str(e),
                    prefix="VFIO",
                )
                if e.errno == errno.EINVAL:
                    log_error_safe(
                        logger,
                        "EINVAL: Invalid argument - group may already be linked or container issue",
                        prefix="VFIO",
                    )
                elif e.errno == errno.EBUSY:
                    log_error_safe(
                        logger,
                        "EBUSY: Group is busy - may be in use by another container",
                        prefix="VFIO",
                    )
                raise OSError(f"Failed to link group {group} to container: {e}")

            # Set the IOMMU type for the container
            try:
                fcntl.ioctl(cont_fd, VFIO_SET_IOMMU, VFIO_TYPE1_IOMMU)
                log_debug_safe(
                    logger, "Set container IOMMU type to Type1", prefix="VFIO"
                )
            except OSError as e:
                log_error_safe(
                    logger, "Failed to set IOMMU type: {e}", e=str(e), prefix="VFIO"
                )
                raise OSError(f"Failed to set IOMMU type to Type1: {e}")

            # Link group to container
            log_debug_safe(
                logger, "Linking group {group} to container", group=group, prefix="VFIO"
            )

            # Verify group is viable
            status = vfio_group_status()
            status.argsz = ctypes.sizeof(status)
            try:
                fcntl.ioctl(grp_fd, VFIO_GROUP_GET_STATUS, status)
            except OSError as e:
                log_error_safe(
                    logger, "Failed to get group status: {e}", e=str(e), prefix="VFIO"
                )
                raise OSError(f"Failed to get group {group} status: {e}")

            if not (status.flags & VFIO_GROUP_FLAGS_VIABLE):
                log_error_safe(
                    logger,
                    "Group {group} is not viable (flags: 0x{flags:x})",
                    group=group,
                    flags=status.flags,
                    prefix="VFIO",
                )
                log_error_safe(logger, "This usually means:", prefix="VFIO")
                log_error_safe(
                    logger,
                    "1. Not all devices in the group are bound to vfio-pci",
                    prefix="VFIO",
                )
                log_error_safe(
                    logger,
                    "2. Some devices in the group are still bound to host drivers",
                    prefix="VFIO",
                )
                log_error_safe(
                    logger, "3. IOMMU group configuration issue", prefix="VFIO"
                )
                raise OSError(
                    f"VFIO group {group} is not viable (flags: 0x{status.flags:x})"
                )

            log_debug_safe(
                logger,
                "Group {group} is viable (flags: 0x{flags:x})",
                group=group,
                flags=status.flags,
                prefix="VFIO",
            )

            # 4. Get device fd from group
            log_debug_safe(
                logger, "Requesting device fd for {bdf}", bdf=bdf, prefix="VFIO"
            )
            # Create a proper ctypes char array for the device name
            name_array = (ctypes.c_char * 40)()
            name_bytes = bdf.encode("utf-8")
            if len(name_bytes) >= 40:
                raise OSError(f"Device name {bdf} too long (max 39 chars)")

            # Copy the device name into the array (null-terminated)
            ctypes.memmove(name_array, name_bytes, len(name_bytes))
            name_array[len(name_bytes)] = 0  # Ensure null termination

            try:
                # Verify device is actually bound to vfio-pci before attempting to get FD
                driver_path = f"/sys/bus/pci/devices/{bdf}/driver"
                if os.path.exists(driver_path):
                    current_driver = os.path.basename(os.readlink(driver_path))
                    if current_driver != "vfio-pci":
                        log_error_safe(
                            logger,
                            f"Device {bdf} is bound to {current_driver}, not vfio-pci",
                            bdf=bdf,
                            current_driver=current_driver,
                            prefix="VFIO",
                        )
                        os.close(cont_fd)
                        raise OSError(
                            f"Device {bdf} not bound to vfio-pci (bound to {current_driver})"
                        )
                else:
                    log_error_safe(
                        logger,
                        "Device {bdf} has no driver binding",
                        bdf=bdf,
                        prefix="VFIO",
                    )
                    os.close(cont_fd)
                    raise OSError(f"Device {bdf} has no driver binding")

                log_debug_safe(
                    logger,
                    "Device {bdf} confirmed bound to vfio-pci",
                    bdf=bdf,
                    prefix="VFIO",
                )

                dev_fd = fcntl.ioctl(grp_fd, VFIO_GROUP_GET_DEVICE_FD, name_array)
                log_info_safe(
                    logger,
                    "Successfully obtained device fd {dev_fd} for {bdf}",
                    dev_fd=dev_fd,
                    bdf=bdf,
                    prefix="VFIO",
                )
                return int(dev_fd), cont_fd

            except OSError as e:
                log_error_safe(
                    logger,
                    "Failed to get device fd for {bdf}: {e}",
                    bdf=bdf,
                    e=str(e),
                    prefix="VFIO",
                )
                if e.errno == errno.EINVAL:
                    log_error_safe(
                        logger,
                        "EINVAL: Invalid argument - device may not be properly bound to vfio-pci or IOMMU group issue",
                        prefix="VFIO",
                    )
                elif e.errno == errno.ENOTTY:
                    log_error_safe(
                        logger,
                        "ENOTTY: Invalid ioctl - check ioctl number calculation",
                        prefix="VFIO",
                    )
                elif e.errno == errno.ENODEV:
                    log_error_safe(
                        logger,
                        "Device {bdf} not found in group {group}",
                        bdf=bdf,
                        group=group,
                        prefix="VFIO",
                    )
                elif e.errno == errno.EBUSY:
                    log_error_safe(
                        logger,
                        "Device {bdf} is busy or already in use",
                        bdf=bdf,
                        prefix="VFIO",
                    )

                # List available devices for debugging
                try:
                    group_devices_path = f"/sys/kernel/iommu_groups/{group}/devices"
                    if os.path.exists(group_devices_path):
                        devices = os.listdir(group_devices_path)
                        log_debug_safe(
                            logger,
                            "Available devices in group {group}: {devices}",
                            group=group,
                            devices=devices,
                            prefix="VFIO",
                        )
                        if bdf not in devices:
                            log_error_safe(
                                logger,
                                "Device {bdf} not in group {group}!",
                                bdf=bdf,
                                group=group,
                                prefix="VFIO",
                            )
                except Exception as list_err:
                    log_warning_safe(
                        logger,
                        "Could not list group devices: {list_err}",
                        list_err=str(list_err),
                        prefix="VFIO",
                    )

                # Close container fd on error
                os.close(cont_fd)
                raise

        except OSError:
            # Close container fd on any error during container setup
            os.close(cont_fd)
            raise

    finally:
        # 5. Close group fd (device fd keeps container reference)
        log_debug_safe(
            logger, "Closing group fd {grp_fd}", grp_fd=grp_fd, prefix="VFIO"
        )
        os.close(grp_fd)
