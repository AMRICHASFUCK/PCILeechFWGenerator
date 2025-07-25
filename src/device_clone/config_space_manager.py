#!/usr/bin/env python3
"""
Configuration Space Management Module

Handles PCI configuration space reading via VFIO and synthetic configuration
space generation for PCILeech firmware building.
"""

import logging
import os
import subprocess
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from ..string_utils import (log_debug_safe, log_error_safe, log_info_safe,
                                log_warning_safe)
except ImportError:
    # Fallback for when string_utils is not available
    def log_info_safe(logger, template, **kwargs):
        logger.info(template.format(**kwargs))

    def log_warning_safe(logger, template, **kwargs):
        logger.warning(template.format(**kwargs))

    def log_error_safe(logger, template, **kwargs):
        logger.error(template.format(**kwargs))

    def log_debug_safe(logger, template, **kwargs):
        logger.debug(template.format(**kwargs))


# Import device configuration system
try:
    from .device_config import DeviceConfiguration, get_device_config
except ImportError:
    # Fallback if device config is not available
    DeviceConfiguration = None

    def get_device_config(
        profile_name: str = "generic",
    ) -> Optional[Any]:
        return None


logger = logging.getLogger(__name__)


# Constants for better maintainability
class ConfigSpaceConstants:
    """PCI Configuration Space constants."""

    # Configuration space sizes
    STANDARD_CONFIG_SIZE = 256
    EXTENDED_CONFIG_SIZE = 4096
    MINIMUM_HEADER_SIZE = 16
    MINIMUM_BAR_SIZE = 40

    # Header offsets
    VENDOR_ID_OFFSET = 0
    DEVICE_ID_OFFSET = 2
    COMMAND_OFFSET = 4
    STATUS_OFFSET = 6
    REVISION_ID_OFFSET = 8
    CLASS_CODE_OFFSET = 9
    CACHE_LINE_SIZE_OFFSET = 12
    LATENCY_TIMER_OFFSET = 13
    HEADER_TYPE_OFFSET = 14
    BIST_OFFSET = 15

    # BAR and subsystem offsets
    BAR_BASE_OFFSET = 16
    BAR_SIZE = 4
    MAX_BARS = 6
    SUBSYS_VENDOR_ID_OFFSET = 44
    SUBSYS_DEVICE_ID_OFFSET = 46
    CAPABILITIES_POINTER_OFFSET = 52

    # Capability offsets and IDs
    MSI_CAPABILITY_ID = 0x05
    MSIX_CAPABILITY_ID = 0x11
    PCIE_CAPABILITY_ID = 0x10

    # Default capability offsets
    MSIX_CAP_OFFSET = 0x40
    MSI_CAP_OFFSET = 0x50
    PCIE_CAP_OFFSET = 0x60
    MSIX_TABLE_OFFSET = 0x100

    # BAR type masks
    BAR_TYPE_MASK = 0x1
    BAR_MEMORY_TYPE_MASK = 0x6
    BAR_PREFETCHABLE_MASK = 0x8
    BAR_64BIT_TYPE = 0x4
    BAR_MEMORY_ADDRESS_MASK = 0xFFFFFFF0
    BAR_IO_ADDRESS_MASK = 0xFFFFFFFC

    # Default values
    DEFAULT_REVISION_ID = 0x01
    DEFAULT_MSIX_TABLE_SIZE = 31  # 32 entries (0-based)


class BarType(IntEnum):
    """BAR type enumeration."""

    MEMORY = 0
    IO = 1


@dataclass
class BarInfo:
    """Structured BAR information."""

    index: int
    bar_type: str
    address: int
    size: int = 0
    prefetchable: bool = False
    is_64bit: bool = False

    @property
    def base_address(self) -> int:
        """Alias for address to maintain compatibility with templates."""
        return self.address

    @property
    def is_memory(self) -> bool:
        """Check if this is a memory BAR."""
        return self.bar_type.lower() == "memory"

    @property
    def is_io(self) -> bool:
        """Check if this is an I/O BAR."""
        return self.bar_type.lower() == "io"

    def __str__(self) -> str:
        bitness = "64-bit" if self.is_64bit else "32-bit"
        prefetch = "prefetchable" if self.prefetchable else "non-prefetchable"
        return f"BAR {self.index}: {self.bar_type} @ 0x{self.address:016x} ({bitness}, {prefetch})"

    def __format__(self, format_spec: str) -> str:
        """Support format operations for template compatibility."""
        if format_spec:
            # If a format spec is provided, format the address
            return format(self.address, format_spec)
        else:
            # Default to string representation
            return str(self)


class ConfigSpaceError(Exception):
    """Base exception for configuration space operations."""

    pass


class VFIOError(ConfigSpaceError):
    """VFIO-specific errors."""

    pass


class SysfsError(ConfigSpaceError):
    """Sysfs-specific errors."""

    pass


class ConfigSpaceManager:
    """Manages PCI configuration space operations with improved structure and error handling."""

    def __init__(
        self, bdf: str, device_profile: str = "generic", strict_vfio: bool = False
    ) -> None:
        """
        Initialize ConfigSpaceManager.

        Args:
            bdf: Bus:Device.Function identifier
            device_profile: Device profile name for configuration
            strict_vfio: If True, require VFIO for config space access
        """
        self.bdf = bdf
        self.device_config = get_device_config(device_profile)
        self.strict_vfio = strict_vfio
        self._config_path = f"/sys/bus/pci/devices/{self.bdf}/config"

    def run_vfio_diagnostics(self) -> None:
        """Run VFIO diagnostics to help troubleshoot issues."""
        try:
            # Try to import and run VFIO diagnostics if available
            import importlib

            vfio_diag_module = importlib.import_module(
                "..cli.vfio_diagnostics", package=__name__
            )
            run_vfio_diagnostics = getattr(
                vfio_diag_module, "run_vfio_diagnostics", None
            )

            if run_vfio_diagnostics:
                log_info_safe(logger, "Running VFIO diagnostics for troubleshooting...")
                run_vfio_diagnostics(self.bdf)
            else:
                log_warning_safe(logger, "VFIO diagnostics function not found")
        except ImportError:
            log_warning_safe(logger, "VFIO diagnostics module not available")
        except Exception as e:
            log_warning_safe(logger, "VFIO diagnostics failed: {error}", error=e)

    def read_vfio_config_space(self, strict: Optional[bool] = None) -> bytes:
        """
        Read PCI configuration space via VFIO with automatic device binding.

        Args:
            strict: If True, fail if VFIO is not available. If None, use instance setting.

        Returns:
            Configuration space bytes

        Raises:
            VFIOError: If VFIO reading fails in strict mode
            SysfsError: If sysfs reading fails in non-strict mode
        """
        if strict is None:
            strict = self.strict_vfio

        log_info_safe(
            logger,
            "Starting config space read for device {bdf}, strict_mode={strict}",
            bdf=self.bdf,
            strict=strict,
            prefix="VFIO",
        )

        if strict:
            return self._read_vfio_strict()
        else:
            return self._read_sysfs_fallback()

    def _read_vfio_strict(self) -> bytes:
        """Read configuration space in strict VFIO mode."""
        try:
            from ..cli.vfio_handler import VFIOBinder

            log_info_safe(
                logger,
                "Binding device {bdf} to VFIO for configuration space access",
                bdf=self.bdf,
            )

            with VFIOBinder(self.bdf) as vfio_device_path:
                log_info_safe(
                    logger,
                    "Successfully bound to VFIO device {vfio_device_path}",
                    vfio_device_path=vfio_device_path,
                )

                # TODO: Implement proper VFIO ioctl for config space access
                # For now, use sysfs method while device is bound to VFIO
                config_space = self._read_sysfs_config_space()

                log_info_safe(
                    logger,
                    "Successfully read {bytes_read} bytes via VFIO",
                    bytes_read=len(config_space),
                    prefix="VFIO",
                )
                log_debug_safe(
                    logger,
                    "First 64 bytes: {first_64_bytes}",
                    first_64_bytes=config_space[:64].hex(),
                    prefix="VFIO",
                )
                return config_space

        except ImportError as e:
            error_msg = f"VFIO module not available: {e}"
            log_error_safe(logger, "{error_msg}", error_msg=error_msg, prefix="VFIO")
            raise VFIOError(f"VFIO config space reading failed: {error_msg}") from e

        except Exception as e:
            error_msg = f"VFIO config space reading failed: {e}"
            log_error_safe(logger, "{error_msg}", error_msg=error_msg, prefix="VFIO")

            self._run_diagnostics_on_error()
            raise VFIOError(f"VFIO config space reading failed: {e}") from e

    def _read_sysfs_fallback(self) -> bytes:
        """Read configuration space using sysfs fallback."""
        try:
            log_info_safe(
                logger,
                "Reading configuration space for device {bdf} via sysfs (non-strict mode)",
                bdf=self.bdf,
                prefix="CNFG",
            )

            config_space = self._read_sysfs_config_space()

            log_info_safe(
                logger,
                "Successfully read {bytes_read} bytes via sysfs",
                bytes_read=len(config_space),
                prefix="CNFG",
            )
            log_debug_safe(
                logger,
                "First 64 bytes: {first_64_bytes}",
                first_64_bytes=config_space[:64].hex(),
                prefix="CNFG",
            )
            return config_space

        except Exception as e:
            log_error_safe(
                logger, "Failed to read sysfs config space: {error}", error=e
            )
            raise SysfsError(
                f"Failed to read configuration space via sysfs: {e}"
            ) from e

    def _run_diagnostics_on_error(self) -> None:
        """Run diagnostics when an error occurs."""
        try:
            log_info_safe(
                logger,
                "Running VFIO diagnostics to help troubleshoot...",
                prefix="VFIO",
            )
            self.run_vfio_diagnostics()
        except Exception as diag_error:
            log_warning_safe(
                logger,
                "Could not run VFIO diagnostics: {error}",
                error=diag_error,
                prefix="VFIO",
            )

    def _read_sysfs_config_space(self) -> bytes:
        """Read configuration space from sysfs with improved error handling."""
        log_info_safe(
            logger,
            "Attempting to read config space from {config_path}",
            config_path=self._config_path,
            prefix="CNFG",
        )

        if not os.path.exists(self._config_path):
            raise SysfsError(f"Config space file not found: {self._config_path}")

        log_info_safe(
            logger,
            "Config space file exists: {config_path}",
            config_path=self._config_path,
            prefix="CNFG",
        )

        try:
            return self._read_config_file_direct()
        except PermissionError:
            log_warning_safe(
                logger,
                "Permission denied when reading config space, trying alternative method",
                prefix="CNFG",
            )
            return self._read_config_file_with_sudo()

    def _read_config_file_direct(self) -> bytes:
        """Read configuration file directly."""
        with open(self._config_path, "rb") as f:
            log_info_safe(
                logger,
                "Reading up to {size} bytes for extended config space",
                size=ConfigSpaceConstants.EXTENDED_CONFIG_SIZE,
                prefix="CNFG",
            )

            data = f.read(ConfigSpaceConstants.EXTENDED_CONFIG_SIZE)

            log_info_safe(
                logger,
                "Successfully read {bytes_read} bytes from sysfs",
                bytes_read=len(data),
                prefix="CNFG",
            )

            return self._validate_and_extend_config_data(data)

    def _read_config_file_with_sudo(self) -> bytes:
        """Read configuration file using sudo hexdump."""
        log_info_safe(
            logger, "Attempting to read config space using sudo hexdump", prefix="CNFG"
        )

        result = subprocess.run(
            ["sudo", "hexdump", "-C", self._config_path],
            capture_output=True,
            text=True,
            check=True,
        )

        hex_data = result.stdout
        log_info_safe(
            logger,
            "Hexdump output length: {length} characters",
            length=len(hex_data),
            prefix="CNFG",
        )

        return self._parse_hexdump_output(hex_data)

    def _validate_and_extend_config_data(self, data: bytes) -> bytes:
        """Validate and extend configuration data if necessary."""
        if len(data) < ConfigSpaceConstants.STANDARD_CONFIG_SIZE:
            log_warning_safe(
                logger,
                "Only read {bytes_read} bytes from config space, minimum required is {min_size}",
                bytes_read=len(data),
                min_size=ConfigSpaceConstants.STANDARD_CONFIG_SIZE,
                prefix="CNFG",
            )

            data = self._extend_config_data(data)

        self._log_device_header_info(data)
        return data

    def _extend_config_data(self, data: bytes) -> bytes:
        """Extend configuration data to minimum required size."""
        extended_data = bytearray(data)

        if len(extended_data) < ConfigSpaceConstants.STANDARD_CONFIG_SIZE:
            padding_bytes = ConfigSpaceConstants.STANDARD_CONFIG_SIZE - len(
                extended_data
            )
            log_warning_safe(
                logger,
                "Padding config space with {padding_bytes} zero bytes",
                padding_bytes=padding_bytes,
                prefix="CNFG",
            )
            extended_data.extend(bytes(padding_bytes))

        # Ensure revision_id is set
        if (
            len(data) <= ConfigSpaceConstants.REVISION_ID_OFFSET
            or extended_data[ConfigSpaceConstants.REVISION_ID_OFFSET] == 0
        ):
            log_warning_safe(
                logger,
                "Revision ID is missing or zero, setting default value 0x{default:02x}",
                default=ConfigSpaceConstants.DEFAULT_REVISION_ID,
                prefix="CNFG",
            )
            extended_data[ConfigSpaceConstants.REVISION_ID_OFFSET] = (
                ConfigSpaceConstants.DEFAULT_REVISION_ID
            )

        log_info_safe(
            logger,
            "Extended config space to {length} bytes",
            length=len(extended_data),
            prefix="CNFG",
        )

        return bytes(extended_data)

    def _log_device_header_info(self, data: bytes) -> None:
        """Log basic device header information."""
        if len(data) >= ConfigSpaceConstants.MINIMUM_HEADER_SIZE:
            vendor_id = int.from_bytes(data[0:2], "little")
            device_id = int.from_bytes(data[2:4], "little")

            log_info_safe(
                logger,
                "Read config space for device {vendor_id:04x}:{device_id:04x}",
                vendor_id=vendor_id,
                device_id=device_id,
                prefix="CNFG",
            )
            log_debug_safe(
                logger,
                "Header bytes 0-15: {header_bytes}",
                header_bytes=data[:16].hex(),
                prefix="CNFG",
            )

    def _parse_hexdump_output(self, hex_data: str) -> bytes:
        """Parse hexdump output to reconstruct binary data."""
        data = bytearray(ConfigSpaceConstants.STANDARD_CONFIG_SIZE)
        bytes_parsed = 0

        for line_num, line in enumerate(hex_data.splitlines()):
            if "|" not in line:
                continue

            parts = line.split("|")[0].strip().split()
            if not parts or not parts[0].endswith(":"):
                continue

            try:
                offset = int(parts[0][:-1], 16)
                hex_values = parts[1:17]  # Up to 16 hex values per line

                for i, hex_val in enumerate(hex_values):
                    if offset + i < len(data):
                        data[offset + i] = int(hex_val, 16)
                        bytes_parsed += 1

            except (ValueError, IndexError) as e:
                log_warning_safe(
                    logger,
                    "Error parsing hexdump line {line_num}: {error}",
                    line_num=line_num,
                    error=e,
                    prefix="CNFG",
                )
                continue

        log_info_safe(
            logger,
            "Parsed {bytes_parsed} bytes from hexdump output",
            bytes_parsed=bytes_parsed,
            prefix="CNFG",
        )

        # Ensure revision_id is set
        if data[ConfigSpaceConstants.REVISION_ID_OFFSET] == 0:
            log_warning_safe(
                logger,
                "Setting default revision ID 0x{default:02x}",
                default=ConfigSpaceConstants.DEFAULT_REVISION_ID,
                prefix="CNFG",
            )
            data[ConfigSpaceConstants.REVISION_ID_OFFSET] = (
                ConfigSpaceConstants.DEFAULT_REVISION_ID
            )

        self._log_device_header_info(bytes(data))
        return bytes(data)

    def generate_synthetic_config_space(self) -> bytes:
        """Generate production-quality synthetic PCI configuration space using device configuration."""
        if not self.device_config:
            raise ConfigSpaceError(
                "Cannot generate synthetic configuration space without device configuration. "
                "Device configuration is required to ensure proper device identity."
            )

        config_space = bytearray(ConfigSpaceConstants.EXTENDED_CONFIG_SIZE)

        try:
            self._populate_basic_header(config_space)
            self._populate_bars(config_space)
            self._populate_subsystem_info(config_space)
            self._populate_capabilities(config_space)
            self._populate_msix_table(config_space)

        except (AttributeError, TypeError) as e:
            raise ConfigSpaceError(
                f"Device configuration is incomplete or invalid: {e}. "
                "Cannot generate synthetic configuration space without complete device data."
            ) from e

        # Safe access to device config attributes
        vendor_id = getattr(
            getattr(self.device_config, "identification", None), "vendor_id", 0
        )
        device_id = getattr(
            getattr(self.device_config, "identification", None), "device_id", 0
        )

        log_info_safe(
            logger,
            "Generated synthetic configuration space: vendor=0x{vendor:04x} device=0x{device:04x}",
            vendor=vendor_id,
            device=device_id,
            prefix="CNFG",
        )

        return bytes(config_space)

    def _populate_basic_header(self, config_space: bytearray) -> None:
        """Populate basic PCI header fields."""
        config = self.device_config

        if not config:
            raise ConfigSpaceError(
                "Device configuration is required to populate header fields."
            )
        if not hasattr(config, "identification") or not hasattr(config, "registers"):
            raise ConfigSpaceError(
                "Device configuration must have 'identification' and 'registers' attributes"
            )
        identification = getattr(config, "identification")
        registers = getattr(config, "registers")

        # Write basic header fields
        config_space[0:2] = getattr(identification, "vendor_id", 0).to_bytes(
            2, "little"
        )
        config_space[2:4] = getattr(identification, "device_id", 0).to_bytes(
            2, "little"
        )
        config_space[4:6] = getattr(registers, "command", 0).to_bytes(2, "little")
        config_space[6:8] = getattr(registers, "status", 0).to_bytes(2, "little")
        config_space[8] = getattr(
            registers, "revision_id", ConfigSpaceConstants.DEFAULT_REVISION_ID
        )
        config_space[9:12] = getattr(identification, "class_code", 0).to_bytes(
            3, "little"
        )
        config_space[12] = getattr(registers, "cache_line_size", 0)
        config_space[13] = getattr(registers, "latency_timer", 0)
        config_space[14] = getattr(registers, "header_type", 0)
        config_space[15] = getattr(registers, "bist", 0)

    def _populate_bars(self, config_space: bytearray) -> None:
        """Populate BAR registers (set to 0 for synthetic config)."""
        for i in range(ConfigSpaceConstants.MAX_BARS):
            bar_offset = ConfigSpaceConstants.BAR_BASE_OFFSET + (
                i * ConfigSpaceConstants.BAR_SIZE
            )
            config_space[bar_offset : bar_offset + ConfigSpaceConstants.BAR_SIZE] = (
                0
            ).to_bytes(4, "little")

    def _populate_subsystem_info(self, config_space: bytearray) -> None:
        """Populate subsystem vendor and device ID."""
        config = self.device_config
        identification = getattr(config, "identification", None)

        if identification:
            subsys_vendor_id = getattr(identification, "subsystem_vendor_id", 0)
            subsys_device_id = getattr(identification, "subsystem_device_id", 0)

            config_space[44:46] = subsys_vendor_id.to_bytes(2, "little")
            config_space[46:48] = subsys_device_id.to_bytes(2, "little")

        # Set capabilities pointer
        config_space[ConfigSpaceConstants.CAPABILITIES_POINTER_OFFSET] = (
            ConfigSpaceConstants.MSIX_CAP_OFFSET
        )

    def _populate_capabilities(self, config_space: bytearray) -> None:
        """Populate PCI capabilities."""
        self._add_msix_capability(config_space)
        self._add_msi_capability(config_space)
        self._add_pcie_capability(config_space)

    def _add_msix_capability(self, config_space: bytearray) -> None:
        """Add MSI-X capability structure."""
        offset = ConfigSpaceConstants.MSIX_CAP_OFFSET

        config_space[offset] = ConfigSpaceConstants.MSIX_CAPABILITY_ID
        config_space[offset + 1] = (
            ConfigSpaceConstants.MSI_CAP_OFFSET
        )  # Next capability pointer

        # Message Control: 32 table entries (5 bits, 0-based)
        table_size = ConfigSpaceConstants.DEFAULT_MSIX_TABLE_SIZE
        config_space[offset + 2 : offset + 4] = (table_size | (0 << 7)).to_bytes(
            2, "little"
        )

        # Table Offset/BIR: BAR 0, offset 0x1000
        table_offset_bir = 0x00001000 | 0x0
        config_space[offset + 4 : offset + 8] = table_offset_bir.to_bytes(4, "little")

        # PBA Offset/BIR: BAR 0, offset 0x2000
        pba_offset_bir = 0x00002000 | 0x0
        config_space[offset + 8 : offset + 12] = pba_offset_bir.to_bytes(4, "little")

    def _add_msi_capability(self, config_space: bytearray) -> None:
        """Add MSI capability structure."""
        offset = ConfigSpaceConstants.MSI_CAP_OFFSET

        config_space[offset] = ConfigSpaceConstants.MSI_CAPABILITY_ID
        config_space[offset + 1] = (
            ConfigSpaceConstants.PCIE_CAP_OFFSET
        )  # Next capability pointer
        config_space[offset + 2] = 0x00  # MSI control register (disabled)
        config_space[offset + 3] = 0x00

    def _add_pcie_capability(self, config_space: bytearray) -> None:
        """Add PCIe capability structure."""
        offset = ConfigSpaceConstants.PCIE_CAP_OFFSET

        config_space[offset] = ConfigSpaceConstants.PCIE_CAPABILITY_ID
        config_space[offset + 1] = 0x00  # No next capability
        config_space[offset + 2] = 0x02  # PCIe capability version 2
        config_space[offset + 3] = 0x00  # Device/port type (endpoint)

    def _populate_msix_table(self, config_space: bytearray) -> None:
        """Add MSI-X table structure in extended config space."""
        for i in range(32):  # 32 MSI-X table entries
            entry_offset = ConfigSpaceConstants.MSIX_TABLE_OFFSET + (i * 16)

            # Message Address (lower and upper 32 bits)
            config_space[entry_offset : entry_offset + 4] = (0).to_bytes(4, "little")
            config_space[entry_offset + 4 : entry_offset + 8] = (0).to_bytes(
                4, "little"
            )

            # Message Data
            config_space[entry_offset + 8 : entry_offset + 12] = (0).to_bytes(
                4, "little"
            )

            # Vector Control (masked)
            config_space[entry_offset + 12 : entry_offset + 16] = (1).to_bytes(
                4, "little"
            )

    def extract_device_info(self, config_space: bytes) -> Dict[str, Any]:
        """Extract device information from configuration space with improved structure."""
        self._validate_config_space_size(config_space)

        device_info = self._extract_basic_device_info(config_space)
        device_info["subsys_vendor_id"], device_info["subsys_device_id"] = (
            self._extract_subsystem_info(config_space)
        )
        device_info["bars"] = self._extract_bar_info(config_space)

        self._log_extracted_device_info(device_info)

        return device_info

    def _validate_config_space_size(self, config_space: bytes) -> None:
        """Validate configuration space has minimum required size."""
        if len(config_space) < ConfigSpaceConstants.MINIMUM_HEADER_SIZE:
            raise ValueError(
                f"Configuration space too short - need at least {ConfigSpaceConstants.MINIMUM_HEADER_SIZE} "
                f"bytes for basic header, got {len(config_space)}"
            )

    def _extract_basic_device_info(self, config_space: bytes) -> Dict[str, Any]:
        """Extract basic device information from configuration space."""
        # Validate required fields exist
        required_offsets = [
            (ConfigSpaceConstants.REVISION_ID_OFFSET, "revision_id"),
            (ConfigSpaceConstants.CLASS_CODE_OFFSET + 2, "class_code"),
            (ConfigSpaceConstants.MINIMUM_HEADER_SIZE - 1, "header fields"),
        ]

        for offset, field_name in required_offsets:
            if len(config_space) <= offset:
                raise ValueError(
                    f"Configuration space too short - missing {field_name} at offset {offset}"
                )

        return {
            "vendor_id": int.from_bytes(config_space[0:2], "little"),
            "device_id": int.from_bytes(config_space[2:4], "little"),
            "command": int.from_bytes(config_space[4:6], "little"),
            "status": int.from_bytes(config_space[6:8], "little"),
            "revision_id": config_space[8],
            "class_code": int.from_bytes(config_space[9:12], "little"),
            "cache_line_size": config_space[12],
            "latency_timer": config_space[13],
            "header_type": config_space[14],
            "bist": config_space[15],
        }

    def _extract_subsystem_info(self, config_space: bytes) -> Tuple[int, int]:
        """Extract subsystem vendor and device IDs."""
        if len(config_space) >= 48:
            subsys_vendor_id = int.from_bytes(config_space[44:46], "little")
            subsys_device_id = int.from_bytes(config_space[46:48], "little")
            return subsys_vendor_id, subsys_device_id
        return 0, 0

    def _extract_bar_info(self, config_space: bytes) -> List[BarInfo]:
        """Extract BAR information with improved structure and error handling."""
        bars = []

        log_info_safe(
            logger,
            "Starting BAR extraction from config space ({length} bytes)",
            length=len(config_space),
            prefix="CNFG",
        )

        if len(config_space) < ConfigSpaceConstants.MINIMUM_BAR_SIZE:
            log_warning_safe(
                logger,
                "Config space too short ({length} bytes) for BAR extraction - need at least {min_size} bytes",
                length=len(config_space),
                min_size=ConfigSpaceConstants.MINIMUM_BAR_SIZE,
                prefix="BARX",
            )
            return bars

        i = 0
        while i < ConfigSpaceConstants.MAX_BARS:
            try:
                bar_info = self._process_single_bar(config_space, i)
                if bar_info:
                    bars.append(bar_info)
                    log_info_safe(
                        logger,
                        "Added BAR {index}: {info}",
                        index=i,
                        info=str(bar_info),
                        prefix="BARS",
                    )

                    # Skip next BAR if this was 64-bit
                    if bar_info.is_64bit:
                        i += 2
                    else:
                        i += 1
                else:
                    i += 1

            except (IndexError, ValueError) as e:
                log_warning_safe(
                    logger,
                    "Error processing BAR {bar_index}: {error}",
                    bar_index=i,
                    error=e,
                    prefix="BARX",
                )
                i += 1
            except KeyboardInterrupt:
                log_warning_safe(
                    logger, "BAR extraction interrupted by user", prefix="BARX"
                )
                raise

        log_info_safe(
            logger,
            "Completed BAR extraction: found {count} active BARs",
            count=len(bars),
            prefix="BARX",
        )

        return bars

    def _process_single_bar(
        self, config_space: bytes, bar_index: int
    ) -> Optional[BarInfo]:
        """Process a single BAR and return BarInfo if active."""
        bar_offset = ConfigSpaceConstants.BAR_BASE_OFFSET + (
            bar_index * ConfigSpaceConstants.BAR_SIZE
        )

        log_debug_safe(
            logger,
            "Processing BAR {index} at offset 0x{offset:02x}",
            index=bar_index,
            offset=bar_offset,
            prefix="BARX",
        )

        if bar_offset + ConfigSpaceConstants.BAR_SIZE > len(config_space):
            log_warning_safe(
                logger,
                "Cannot read BAR {bar_index} - insufficient config space length",
                bar_index=bar_index,
                prefix="BARX",
            )
            return None

        bar_value = int.from_bytes(config_space[bar_offset : bar_offset + 4], "little")

        log_debug_safe(
            logger,
            "BAR {index} raw value: 0x{value:08x}",
            index=bar_index,
            value=bar_value,
            prefix="BARX",
        )

        if bar_value == 0:
            log_debug_safe(
                logger,
                "BAR {index} is empty (zero value), skipping",
                index=bar_index,
                prefix="BARX",
            )
            return None

        log_info_safe(
            logger,
            "BAR {index} is active (non-zero): 0x{value:08x}",
            index=bar_index,
            value=bar_value,
            prefix="BARX",
        )

        # Decode BAR type and properties
        bar_type = (
            "io" if (bar_value & ConfigSpaceConstants.BAR_TYPE_MASK) else "memory"
        )
        bar_prefetchable = (
            bool(bar_value & ConfigSpaceConstants.BAR_PREFETCHABLE_MASK)
            if bar_type == "memory"
            else False
        )
        bar_64bit = (
            (
                (bar_value & ConfigSpaceConstants.BAR_MEMORY_TYPE_MASK)
                == ConfigSpaceConstants.BAR_64BIT_TYPE
            )
            if bar_type == "memory"
            else False
        )

        log_info_safe(
            logger,
            "BAR {index} properties: type={type}, prefetchable={prefetchable}, is_64bit={is_64bit}",
            index=bar_index,
            type=bar_type,
            prefetchable=bar_prefetchable,
            is_64bit=bar_64bit,
            prefix="BARX",
        )

        # Calculate base address
        if bar_type == "memory":
            bar_addr = bar_value & ConfigSpaceConstants.BAR_MEMORY_ADDRESS_MASK
        else:
            bar_addr = bar_value & ConfigSpaceConstants.BAR_IO_ADDRESS_MASK

        log_debug_safe(
            logger,
            "BAR {index} base address (lower 32-bit): 0x{address:08x}",
            index=bar_index,
            address=bar_addr,
            prefix="BARX",
        )

        # For 64-bit BARs, read the next BAR as well
        if bar_64bit and bar_index < ConfigSpaceConstants.MAX_BARS - 1:
            next_bar_offset = bar_offset + ConfigSpaceConstants.BAR_SIZE
            log_debug_safe(
                logger,
                "Reading upper 32-bit for 64-bit BAR {index} at offset 0x{offset:02x}",
                index=bar_index,
                offset=next_bar_offset,
                prefix="BARX",
            )

            if next_bar_offset + ConfigSpaceConstants.BAR_SIZE <= len(config_space):
                next_bar_value = int.from_bytes(
                    config_space[
                        next_bar_offset : next_bar_offset
                        + ConfigSpaceConstants.BAR_SIZE
                    ],
                    "little",
                )
                log_debug_safe(
                    logger,
                    "BAR {index} upper 32-bit value: 0x{next_bar_value:08x}",
                    index=bar_index,
                    next_bar_value=next_bar_value,
                    prefix="BARX",
                )
                bar_addr |= next_bar_value << 32
                log_info_safe(
                    logger,
                    "BAR {index} full 64-bit address: 0x{address:016x}",
                    index=bar_index,
                    address=bar_addr,
                    prefix="BARX",
                )
            else:
                log_warning_safe(
                    logger,
                    "Cannot read upper 32-bit for BAR {bar_index} - insufficient config space",
                    bar_index=bar_index,
                    prefix="BARX",
                )

        return BarInfo(
            index=bar_index,
            bar_type=bar_type,
            address=bar_addr,
            size=0,  # Size would need to be determined by probing
            prefetchable=bar_prefetchable,
            is_64bit=bar_64bit,
        )

    def _log_extracted_device_info(self, device_info: Dict[str, Any]) -> None:
        """Log extracted device information in a structured way."""
        vendor_id = device_info["vendor_id"]
        device_id = device_info["device_id"]
        class_code = device_info["class_code"]
        revision_id = device_info["revision_id"]
        command = device_info["command"]
        status = device_info["status"]
        header_type = device_info["header_type"]
        subsys_vendor_id = device_info["subsys_vendor_id"]
        subsys_device_id = device_info["subsys_device_id"]
        cache_line_size = device_info["cache_line_size"]
        latency_timer = device_info["latency_timer"]
        bist = device_info["bist"]
        bars = device_info["bars"]

        log_info_safe(logger, "Successfully extracted device information:")
        log_info_safe(logger, "  Vendor ID: 0x{vendor_id:04x}", vendor_id=vendor_id)
        log_info_safe(logger, "  Device ID: 0x{device_id:04x}", device_id=device_id)
        log_info_safe(logger, "  Class Code: 0x{class_code:06x}", class_code=class_code)
        log_info_safe(
            logger, "  Revision ID: 0x{revision_id:02x}", revision_id=revision_id
        )
        log_info_safe(logger, "  Command: 0x{command:04x}", command=command)
        log_info_safe(logger, "  Status: 0x{status:04x}", status=status)
        log_info_safe(
            logger, "  Header Type: 0x{header_type:02x}", header_type=header_type
        )
        log_info_safe(
            logger,
            "  Subsystem Vendor: 0x{subsys_vendor_id:04x}",
            subsys_vendor_id=subsys_vendor_id,
        )
        log_info_safe(
            logger,
            "  Subsystem Device: 0x{subsys_device_id:04x}",
            subsys_device_id=subsys_device_id,
        )
        log_info_safe(
            logger,
            "  Cache Line Size: {cache_line_size}",
            cache_line_size=cache_line_size,
        )
        log_info_safe(
            logger, "  Latency Timer: {latency_timer}", latency_timer=latency_timer
        )
        log_info_safe(logger, "  BIST: 0x{bist:02x}", bist=bist)
        log_info_safe(logger, "  Total BARs found: {count}", count=len(bars))

        # Log detailed BAR summary
        if bars:
            log_info_safe(
                logger, "Active BARs found: {count}", count=len(bars), prefix="BARS"
            )
            for bar in bars:
                log_info_safe(logger, str(bar), prefix="BARS")
        else:
            log_info_safe(
                logger,
                "No active BARs found - this may indicate a misconfigured or non-functional device",
                prefix="BARS",
            )

        log_info_safe(
            logger,
            "Extracted device info: vendor={vendor:04x} device={device:04x} class={class_code:06x}",
            vendor=vendor_id,
            device=device_id,
            class_code=class_code,
            prefix="DEVI",
        )
