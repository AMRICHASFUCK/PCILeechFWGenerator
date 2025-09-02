#!/usr/bin/env python3
"""Tests for --allow-msix-placeholder flag wiring in pcileech.py."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest  # type: ignore

# Add project root to path for imports
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pcileech


class TestMsixPlaceholderFlag:
    def test_flag_propagated_to_cli(self):
        with patch("pcileech.get_available_boards", return_value=["mock_board"]), \
             patch("pcileech.get_logger", return_value=MagicMock()), \
             patch("pcileech.check_sudo", return_value=True), \
             patch("pcileech.check_vfio_requirements", return_value=True), \
             patch("src.cli.cli.main", return_value=0) as mock_cli_main:
            parser = pcileech.create_parser()
            args = parser.parse_args([
                "build",
                "--bdf",
                "0000:00:00.0",
                "--board",
                "mock_board",
                "--allow-msix-placeholder",
            ])

            pcileech.handle_build(args)

            mock_cli_main.assert_called_once_with([
                "build",
                "--bdf",
                "0000:00:00.0",
                "--board",
                "mock_board",
                "--allow-msix-placeholder",
            ])
