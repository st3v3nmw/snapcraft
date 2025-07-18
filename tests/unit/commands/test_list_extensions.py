# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2022,2024 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from argparse import Namespace
from textwrap import dedent

import pytest

import snapcraft.commands


@pytest.mark.usefixtures("fake_extension")
@pytest.mark.parametrize(
    "command",
    [
        snapcraft.commands.ListExtensionsCommand,
        snapcraft.commands.ExtensionsCommand,
    ],
)
def test_command(emitter, command, fake_app_config):
    cmd = command(fake_app_config)

    cmd.run(Namespace())

    if cmd.hidden:
        emitter.assert_progress(
            f"The '{cmd.name}' command was renamed to 'list-extensions'. Use 'list-extensions' instead. "
            "The old name will be removed in a future release.",
            permanent=True,
        )
    emitter.assert_message(
        dedent(
            """\
        Extension name          Supported bases
        ----------------------  ------------------------------
        env-injector            core24
        fake-extension          core22, core24
        flutter-beta            core18
        flutter-dev             core18
        flutter-master          core18
        flutter-stable          core18
        gnome                   core22, core24
        gnome-3-28              core18
        gnome-3-34              core18
        gnome-3-38              core20
        kde-neon                core18, core20, core22, core24
        kde-neon-6              core22, core24
        kde-neon-qt6            core22, core24
        ros1-noetic             core20
        ros1-noetic-desktop     core20
        ros1-noetic-perception  core20
        ros1-noetic-robot       core20
        ros1-noetic-ros-base    core20
        ros1-noetic-ros-core    core20
        ros2-foxy               core20
        ros2-foxy-desktop       core20
        ros2-foxy-ros-base      core20
        ros2-foxy-ros-core      core20
        ros2-humble             core22
        ros2-humble-desktop     core22
        ros2-humble-ros-base    core22
        ros2-humble-ros-core    core22
        ros2-jazzy              core24
        ros2-jazzy-desktop      core24
        ros2-jazzy-ros-base     core24
        ros2-jazzy-ros-core     core24"""
        )
    )


@pytest.mark.usefixtures("fake_extension_name_from_legacy")
@pytest.mark.parametrize(
    "command",
    [
        snapcraft.commands.ListExtensionsCommand,
        snapcraft.commands.ExtensionsCommand,
    ],
)
def test_command_extension_dups(emitter, command, fake_app_config):
    cmd = command(fake_app_config)
    cmd.run(Namespace())
    emitter.assert_message(
        dedent(
            """\
        Extension name          Supported bases
        ----------------------  ------------------------------
        env-injector            core24
        flutter-beta            core18
        flutter-dev             core18
        flutter-master          core18
        flutter-stable          core18
        gnome                   core22, core24
        gnome-3-28              core18
        gnome-3-34              core18
        gnome-3-38              core20
        kde-neon                core18, core20, core22, core24
        kde-neon-6              core22, core24
        kde-neon-qt6            core22, core24
        ros1-noetic             core20
        ros1-noetic-desktop     core20
        ros1-noetic-perception  core20
        ros1-noetic-robot       core20
        ros1-noetic-ros-base    core20
        ros1-noetic-ros-core    core20
        ros2-foxy               core20
        ros2-foxy-desktop       core20
        ros2-foxy-ros-base      core20
        ros2-foxy-ros-core      core20
        ros2-humble             core22
        ros2-humble-desktop     core22
        ros2-humble-ros-base    core22
        ros2-humble-ros-core    core22
        ros2-jazzy              core24
        ros2-jazzy-desktop      core24
        ros2-jazzy-ros-base     core24
        ros2-jazzy-ros-core     core24"""
        )
    )
