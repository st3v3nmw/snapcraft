# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2022-2024 Canonical Ltd.
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

"""Parts lifecycle preparation and execution."""

import copy
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import craft_parts
from craft_cli import emit
from craft_parts import Features, ProjectInfo, Step, StepInfo, callbacks
from craft_platforms import DebianArchitecture
from craft_providers import Executor

from snapcraft import errors, linters, models, pack, providers, ua_manager, utils
from snapcraft.elf import Patcher, SonameCache, elf_utils
from snapcraft.elf import errors as elf_errors
from snapcraft.linters import LinterStatus
from snapcraft.meta import component_yaml, manifest, snap_yaml
from snapcraft.utils import process_version

from . import yaml_utils
from .parts import PartsLifecycle, launch_shell
from .project_check import run_project_checks
from .setup_assets import setup_assets
from .update_metadata import update_project_metadata

if TYPE_CHECKING:
    import argparse


_EXPERIMENTAL_PLUGINS = ["kernel", "matter-sdk"]


def run(command_name: str, parsed_args: "argparse.Namespace") -> None:
    """Run the parts lifecycle.

    :raises SnapcraftError: if the step name is invalid, or the project
        yaml file cannot be loaded.
    :raises LegacyFallback: if the project's base is core20 or below.
    """
    emit.debug(f"command: {command_name}, arguments: {parsed_args}")

    snap_project = yaml_utils.get_snap_project()
    yaml_data = yaml_utils.process_yaml(snap_project.project_file)
    start_time = datetime.now()

    if parsed_args.provider:
        raise errors.SnapcraftError("Option --provider is not supported.")

    if yaml_data.get("ua-services"):
        if not parsed_args.ua_token:
            raise errors.SnapcraftError(
                "UA services require a UA token to be specified."
            )

        if not parsed_args.enable_experimental_ua_services:
            raise errors.SnapcraftError(
                "Using UA services requires --enable-experimental-ua-services."
            )

    build_plan = get_build_plan(yaml_data, parsed_args)

    # Register our own callbacks
    callbacks.register_prologue(set_global_environment)
    callbacks.register_pre_step(set_step_environment)
    callbacks.register_post_step(patch_elf, step_list=[Step.PRIME])

    build_count = utils.get_parallel_build_count()

    partitions = _validate_and_get_partitions(yaml_data)

    _warn_on_multiple_builds(parsed_args, build_plan)

    for build_on, build_for in build_plan:
        emit.verbose(f"Running on {build_on} for {build_for}")
        yaml_data_for_arch = yaml_utils.apply_yaml(yaml_data, build_on, build_for)
        parse_info = yaml_utils.extract_parse_info(yaml_data_for_arch)
        _expand_environment(
            yaml_data_for_arch,
            parallel_build_count=build_count,
            target_arch=build_for,
            partitions=partitions,
        )
        project = models.Project.unmarshal(yaml_data_for_arch)

        _run_command(
            command_name,
            project=project,
            parse_info=parse_info,
            parallel_build_count=build_count,
            assets_dir=snap_project.assets_dir,
            start_time=start_time,
            parsed_args=parsed_args,
        )


def _run_command(  # noqa PLR0913 (too-many-arguments)
    command_name: str,
    *,
    project: models.Project,
    parse_info: dict[str, list[str]],
    assets_dir: Path,
    start_time: datetime,
    parallel_build_count: int,
    parsed_args: "argparse.Namespace",
) -> None:
    managed_mode = utils.is_managed_mode()
    part_names = getattr(parsed_args, "parts", None)

    enable_experimental_plugins = getattr(
        parsed_args, "enable_experimental_plugins", False
    )
    _check_experimental_plugins(project, enable_experimental_plugins)

    if not managed_mode:
        run_project_checks(project, assets_dir=assets_dir)

    if _is_manager(parsed_args):
        if command_name == "clean" and not part_names:
            _clean_provider(project, parsed_args)
        else:
            _run_in_provider(project, command_name, parsed_args)
        return

    if managed_mode:
        work_dir = utils.get_managed_environment_home_path()
        project_dir = utils.get_managed_environment_project_path()
    else:
        work_dir = project_dir = Path.cwd()

    step_name = "prime" if command_name in ("pack", "snap", "try") else command_name

    track_stage_packages = getattr(parsed_args, "enable_manifest", False)

    lifecycle = PartsLifecycle(
        project.parts,
        work_dir=work_dir,
        assets_dir=assets_dir,
        base=project.get_effective_base(),
        project_base=project.base or "",
        confinement=project.confinement,
        package_repositories=project.package_repositories or [],
        parallel_build_count=parallel_build_count,
        part_names=part_names,
        adopt_info=project.adopt_info,
        project_name=project.name,
        parse_info=parse_info,
        project_vars={
            "version": project.version or "",
            "grade": project.grade or "",
        },
        extra_build_snaps=project.get_extra_build_snaps(),
        target_arch=project.get_build_for(),
        track_stage_packages=track_stage_packages,
        partitions=project.get_partitions(),
    )

    if command_name == "clean":
        lifecycle.clean(part_names=part_names)
        return

    # patchelf relies on known file list to work properly, but override-prime
    # parts don't have the list yet, and we can't track changes in the prime.
    for _part in getattr(project, "parts", {}).values():
        if "enable-patchelf" in _part.get("build-attributes", []) and _part.get(
            "override-prime", None
        ):
            emit.progress(
                "Warning: 'enable-patchelf' feature will not apply to files primed "
                "by parts that use the 'override-prime' keyword. It's not possible "
                "to track file changes in the prime directory.",
                permanent=True,
            )

    try:
        _run_lifecycle_and_pack(
            lifecycle,
            command_name=command_name,
            step_name=step_name,
            project=project,
            project_dir=project_dir,
            assets_dir=assets_dir,
            start_time=start_time,
            parsed_args=parsed_args,
        )
    except PermissionError as err:
        if parsed_args.debug:
            emit.progress(str(err), permanent=True)
            launch_shell()
        # Casting as a str as OSError should always contain an error message
        raise errors.FilePermissionError(err.filename, reason=cast(str, err.strerror))
    except OSError as err:
        msg = err.strerror
        if err.filename:
            msg = f"{err.filename}: {msg}"
        if parsed_args.debug:
            emit.progress(msg, permanent=True)
            launch_shell()
        # Casting as a str as OSError should always contain an error message
        raise errors.SnapcraftError(cast(str, msg)) from err
    except errors.SnapcraftError as err:
        if parsed_args.debug:
            emit.progress(str(err), permanent=True)
            launch_shell()
        raise
    except Exception as err:
        if parsed_args.debug:
            emit.progress(str(err), permanent=True)
            launch_shell()
        raise errors.SnapcraftError(str(err)) from err


def _run_lifecycle_and_pack(  # noqa PLR0913
    lifecycle: PartsLifecycle,
    *,
    command_name: str,
    step_name: str,
    project: models.Project,
    project_dir: Path,
    assets_dir: Path,
    start_time: datetime,
    parsed_args: "argparse.Namespace",
) -> None:
    """Execute the parts lifecycle, generate metadata, and create the snap."""
    with ua_manager.ua_manager(parsed_args.ua_token, services=project.ua_services):
        lifecycle.run(
            step_name,
            shell=getattr(parsed_args, "shell", False),
            shell_after=getattr(parsed_args, "shell_after", False),
            # Repriming needs to happen to take into account any changes to
            # the actual target directory.
            rerun_step=command_name == "try",
        )

    # Extract metadata and generate snap.yaml
    part_names = getattr(parsed_args, "part_names", None)

    if step_name == "prime" and not part_names:
        _generate_metadata(
            project=project,
            lifecycle=lifecycle,
            project_dir=project_dir,
            assets_dir=assets_dir,
            start_time=start_time,
            parsed_args=parsed_args,
        )

    if command_name in ("pack", "snap"):
        issues = linters.run_linters(lifecycle.prime_dir, lint=project.lint)
        status = linters.report(issues, intermediate=True)

        # In case of linter errors, stop execution and return the error code.
        if status in (LinterStatus.ERRORS, LinterStatus.FATAL):
            raise errors.LinterError("Linter errors found", exit_code=status)

        snap_filename = pack.pack_snap(
            lifecycle.prime_dir,
            output=parsed_args.output,
            compression=project.compression,
            name=project.name,
            version=process_version(project.version),
            target=project.get_build_for(),
        )
        emit.progress(f"Created snap package {snap_filename}", permanent=True)

        if project.components:
            _pack_components(lifecycle, project, parsed_args.output)


def _pack_components(
    lifecycle: PartsLifecycle, project: models.Project, output: str | None
) -> None:
    """Pack components.

    `--output` can be used to set the output directory, the name of the snap, or both.

    If `output` is a directory, output components in the output directory.
    If `output` is a filename, output components in the parent directory.
    If `output` is not provided, output components in the cwd.

    :param lifecycle: The part lifecycle.
    :param project: The snapcraft project.
    :param output: Output filepath of snap.
    """
    emit.progress("Creating component packages...")

    if output:
        if Path(output).is_dir():
            output_dir = Path(output).resolve()
        else:
            output_dir = Path(output).parent.resolve()
    else:
        output_dir = Path.cwd()

    for component in project.get_component_names():
        filename = pack.pack_component(
            directory=lifecycle.get_prime_dir(component),
            compression=project.compression,
            output_dir=output_dir,
        )
        emit.verbose(f"Packed component {component!r} to {filename!r}.")
    emit.progress("Created component packages", permanent=True)


def _generate_metadata(
    *,
    project: models.Project,
    lifecycle: PartsLifecycle,
    project_dir: Path,
    assets_dir: Path,
    start_time: datetime,
    parsed_args: "argparse.Namespace",
):
    project_vars = lifecycle.project_vars

    emit.progress("Extracting and updating metadata...")
    metadata_list = lifecycle.extract_metadata()
    update_project_metadata(
        project,
        project_vars=project_vars,
        metadata_list=metadata_list,
        assets_dir=assets_dir,
        prime_dir=lifecycle.prime_dir,
    )

    emit.progress("Copying snap assets...")
    setup_assets(
        project,
        assets_dir=assets_dir,
        project_dir=project_dir,
        prime_dirs=lifecycle.prime_dirs,
    )

    emit.progress("Generating snap metadata...")
    snap_yaml.write(project, lifecycle.prime_dir, arch=project.get_build_for())
    emit.progress("Generated snap metadata", permanent=True)

    if components := project.get_component_names():
        emit.progress("Generating component metadata...")
        for component in components:
            component_yaml.write(
                project=project,
                component_name=component,
                component_prime_dir=lifecycle.get_prime_dir(component),
            )
        emit.progress("Generated component metadata", permanent=True)

    if parsed_args.enable_manifest:
        emit.progress(
            "'--enable-manifest' is deprecated, and will be removed in core24.",
            permanent=True,
        )
        _generate_manifest(
            project,
            lifecycle=lifecycle,
            start_time=start_time,
            parsed_args=parsed_args,
        )


def _generate_manifest(
    project: models.Project,
    *,
    lifecycle: PartsLifecycle,
    start_time: datetime,
    parsed_args: "argparse.Namespace",
) -> None:
    """Create and populate the manifest file."""
    emit.progress("Generating snap manifest...")
    image_information = parsed_args.manifest_image_information or "{}"

    parts = copy.deepcopy(project.parts)
    for name, part in parts.items():
        assets = lifecycle.get_part_pull_assets(part_name=name)
        if assets:
            part["stage-packages"] = assets.get("stage-packages", []) or []
        for key in ("stage", "prime", "stage-packages", "build-packages"):
            part.setdefault(key, [])

    manifest.write(
        project,
        lifecycle.prime_dir,
        arch=project.get_build_for(),
        parts=parts,
        start_time=start_time,
        image_information=image_information,
        primed_stage_packages=lifecycle.get_primed_stage_packages(),
    )
    emit.progress("Generated snap manifest", permanent=True)

    # Also copy the original snapcraft.yaml
    snap_project = yaml_utils.get_snap_project()
    shutil.copy(snap_project.project_file, lifecycle.prime_dir / "snap")


def _clean_provider(project: models.Project, parsed_args: "argparse.Namespace") -> None:
    """Clean the provider environment.

    :param project: The project to clean.
    """
    emit.progress("Cleaning build provider")
    provider_name = "lxd" if parsed_args.use_lxd else None
    provider = providers.get_provider(provider_name)
    instance_name = providers.get_instance_name(
        project_name=project.name,
        project_path=Path().absolute(),
        build_on=project.get_build_on(),
        build_for=project.get_build_for(),
    )
    emit.debug(f"Cleaning instance {instance_name}")
    provider.clean_project_environments(instance_name=instance_name)
    emit.progress("Cleaned build provider", permanent=True)


def _run_in_provider(  # noqa PLR0915
    project: models.Project, command_name: str, parsed_args: "argparse.Namespace"
) -> None:
    """Pack image in provider instance."""
    emit.debug("Checking build provider availability")
    provider_name = "lxd" if parsed_args.use_lxd else None
    provider = providers.get_provider(provider_name)
    providers.ensure_provider_is_available(provider)

    cmd = ["snapcraft", command_name]

    if hasattr(parsed_args, "parts"):
        cmd.extend(parsed_args.parts)

    if getattr(parsed_args, "output", None):
        cmd.extend(["--output", parsed_args.output])

    mode = emit.get_mode().name.lower()
    cmd.append(f"--verbosity={mode}")

    if parsed_args.debug:
        cmd.append("--debug")
    if getattr(parsed_args, "shell", False):
        cmd.append("--shell")
    if getattr(parsed_args, "shell_after", False):
        cmd.append("--shell-after")

    if getattr(parsed_args, "enable_manifest", False):
        cmd.append("--enable-manifest")
        emit.progress(
            "'--enable-manifest' is deprecated, and will be removed in core24.",
            permanent=True,
        )
    image_information = getattr(parsed_args, "manifest_image_information", None)
    if image_information:
        cmd.extend(["--manifest-image-information", image_information])
        emit.progress(
            "'--manifest-image-information' is deprecated, and will be removed in core24.",
            permanent=True,
        )

    cmd.append("--build-for")
    cmd.append(project.get_build_for())

    ua_token = getattr(parsed_args, "ua_token", "")
    if ua_token:
        cmd.extend(["--ua-token", ua_token])

    if getattr(parsed_args, "enable_experimental_ua_services", False):
        cmd.append("--enable-experimental-ua-services")

    if getattr(parsed_args, "enable_experimental_plugins", False):
        cmd.append("--enable-experimental-plugins")

    project_path = Path().absolute()
    output_dir = utils.get_managed_environment_project_path()

    instance_name = providers.get_instance_name(
        project_name=project.name,
        project_path=project_path,
        build_on=project.get_build_on(),
        build_for=project.get_build_for(),
    )

    snapcraft_base = project.get_effective_base()
    build_base = providers.SNAPCRAFT_BASE_TO_PROVIDER_BASE[snapcraft_base]

    if snapcraft_base in ("devel", "core24"):
        emit.progress(
            "Running snapcraft with a devel instance is for testing purposes only.",
            permanent=True,
        )
        allow_unstable = True
    else:
        allow_unstable = False

    base_configuration = providers.get_base_configuration(
        alias=build_base,
        instance_name=instance_name,
        http_proxy=parsed_args.http_proxy,
        https_proxy=parsed_args.https_proxy,
    )

    emit.progress("Launching instance...")
    with provider.launched_environment(
        project_name=project.name,
        project_path=project_path,
        base_configuration=base_configuration,
        instance_name=instance_name,
        allow_unstable=allow_unstable,
    ) as instance:
        try:
            providers.prepare_instance(
                instance=instance,
                host_project_path=project_path,
                bind_ssh=parsed_args.bind_ssh,
            )
            with emit.pause():
                if command_name == "try":
                    _expose_prime(project_path, instance, project.get_partitions())
                # run snapcraft inside the instance
                instance.execute_run(cmd, check=True, cwd=output_dir)
        except subprocess.CalledProcessError as err:
            raise errors.SnapcraftError(
                f"Failed to execute {command_name} in instance.",
                details=err.stderr.strip() if err.stderr else None,
                resolution=(
                    "Run the same command again with --debug to shell into "
                    "the environment if you wish to introspect this failure."
                ),
            ) from err
        finally:
            providers.capture_logs_from_instance(instance)


def _expose_prime(project_path: Path, instance: Executor, partitions: list[str] | None):
    """Expose the instance's prime directory in ``project_path`` on the host.

    :param project_path: path of the project
    :param instance: instance with the prime directory to expose
    :param partitions: A list of partitions for the project.
    """
    host_prime = project_path / "prime"
    host_prime.mkdir(exist_ok=True)

    managed_root = utils.get_managed_environment_home_path()
    dirs = craft_parts.ProjectDirs(work_dir=managed_root, partitions=partitions)

    instance.mount(host_source=project_path / "prime", target=dirs.prime_dir)


def set_global_environment(info: ProjectInfo) -> None:
    """Set global environment variables."""
    info.global_environment.update(
        {
            "SNAPCRAFT_PARALLEL_BUILD_COUNT": str(info.parallel_build_count),
            "SNAPCRAFT_PROJECT_VERSION": info.get_project_var("version", raw_read=True),
            "SNAPCRAFT_PROJECT_GRADE": info.get_project_var("grade", raw_read=True),
            "SNAPCRAFT_PROJECT_DIR": str(info.project_dir),
            "SNAPCRAFT_PROJECT_NAME": str(info.project_name),
            "SNAPCRAFT_STAGE": str(info.stage_dir),
            "SNAPCRAFT_PRIME": str(info.prime_dir),
        }
    )

    # add deprecated environment variables for core22
    if info.base == "core22":
        info.global_environment.update(
            {
                "SNAPCRAFT_ARCH_TRIPLET": info.arch_triplet,
                "SNAPCRAFT_TARGET_ARCH": info.target_arch,
            }
        )

    if info.partitions:
        info.global_environment.update(_get_environment_for_partitions(info))


def _get_environment_for_partitions(info: ProjectInfo) -> dict[str, str]:
    """Get environment variables related to partitions.

    Assumes the partition feature is enabled and partitions are defined.

    :param info: The project information.

    :returns: A dictionary contain environment variables for partitions.
    """
    environment: dict[str, str] = {}

    if not info.partitions:
        raise ValueError("Project does not contain any partitions.")

    for partition in info.partitions:
        formatted_partition = partition.upper().translate(
            {ord("-"): "_", ord("/"): "_"}
        )

        environment[f"SNAPCRAFT_{formatted_partition}_STAGE"] = str(
            info.get_stage_dir(partition=partition)
        )
        environment[f"SNAPCRAFT_{formatted_partition}_PRIME"] = str(
            info.get_prime_dir(partition=partition)
        )

    return environment


def _check_experimental_plugins(
    project: models.Project, enable_experimental_plugins: bool
) -> None:
    """Ensure the experimental plugin flag is enabled to use unstable plugins."""
    for name, part in project.parts.items():
        if not isinstance(part, dict):
            continue

        plugin = part.get("plugin", "")
        if plugin not in _EXPERIMENTAL_PLUGINS:
            continue

        if enable_experimental_plugins:
            emit.progress(f"*EXPERIMENTAL* plugin '{name}' enabled", permanent=True)
            continue

        raise errors.SnapcraftError(
            f"Plugin '{plugin}' in part '{name}' is unstable and may change in the future.",
            resolution="Rerun with --enable-experimental-plugins to use this plugin.",
        )


def set_step_environment(step_info: StepInfo) -> bool:
    """Set the step environment before executing each lifecycle step."""
    step_info.step_environment.update(
        {
            "SNAPCRAFT_PART_SRC": str(step_info.part_src_dir),
            "SNAPCRAFT_PART_SRC_WORK": str(step_info.part_src_subdir),
            "SNAPCRAFT_PART_BUILD": str(step_info.part_build_dir),
            "SNAPCRAFT_PART_BUILD_WORK": str(step_info.part_build_subdir),
            "SNAPCRAFT_PART_INSTALL": str(step_info.part_install_dir),
        }
    )
    return True


def patch_elf(step_info: StepInfo, use_system_libs: bool = True) -> bool:
    """Patch rpath and interpreter in ELF files for classic mode.

    :param step_info: The step information.
    :param use_system_libs: If true, search for dependencies in the default
        library search paths.

    :returns: True

    :raises DynamicLinkerNotFound: If the dynamic linker is not found.
    :raises PatcherError: If the ELF file cannot be patched.
    """
    if "enable-patchelf" not in step_info.build_attributes:
        emit.debug(f"patch_elf: not enabled for part {step_info.part_name!r}")
        return True

    if not step_info.state:
        emit.debug("patch_elf: no state information")
        return True

    try:
        # If libc is staged we'll find a dynamic linker in the payload. At
        # runtime the linker will be in the installed snap path.
        linker = elf_utils.get_dynamic_linker(
            root_path=step_info.prime_dir,
            snap_path=Path(f"/snap/{step_info.project_name}/current"),
        )
    except elf_errors.DynamicLinkerNotFound:
        # Otherwise look for the host linker, which should match the base
        # system linker. At runtime use the linker from the installed base
        # snap.
        linker = elf_utils.get_dynamic_linker(
            root_path=Path("/"), snap_path=Path(f"/snap/{step_info.base}/current")
        )

    migrated_files = step_info.state.files
    patcher = Patcher(dynamic_linker=linker, root_path=step_info.prime_dir)
    elf_files = elf_utils.get_elf_files_from_list(step_info.prime_dir, migrated_files)
    soname_cache = SonameCache()
    arch_triplet = elf_utils.get_arch_triplet()

    for elf_file in elf_files:
        elf_file.load_dependencies(
            root_path=step_info.prime_dir,
            base_path=Path(f"/snap/{step_info.base}/current"),
            content_dirs=[],  # classic snaps don't use content providers
            arch_triplet=arch_triplet,
            soname_cache=soname_cache,
        )

        relative_path = elf_file.path.relative_to(step_info.prime_dir)
        emit.progress(f"Patch ELF file: {str(relative_path)!r}")
        patcher.patch(elf_file=elf_file, use_system_libs=use_system_libs)

    return True


def _expand_environment(
    snapcraft_yaml: dict[str, Any],
    *,
    parallel_build_count: int,
    target_arch: str,
    partitions: list[str] | None,
) -> None:
    """Expand global variables in the provided dictionary values.

    :param snapcraft_yaml: A dictionary containing the contents of the
        snapcraft.yaml project file.
    :param parallel_build_count: The maximum number of concurrent jobs.
    :param target_arch: The target architecture of the project.
    :param partitions: A list of partitions for the project.
    """
    if utils.is_managed_mode():
        work_dir = utils.get_managed_environment_home_path()
    else:
        work_dir = Path.cwd()

    project_vars = {
        "version": snapcraft_yaml.get("version", ""),
        "grade": snapcraft_yaml.get("grade", ""),
    }

    if target_arch == "all":
        target_arch = str(DebianArchitecture.from_host())

    dirs = craft_parts.ProjectDirs(work_dir=work_dir, partitions=partitions)
    info = craft_parts.ProjectInfo(
        application_name="snapcraft",  # not used in environment expansion
        base=yaml_utils.get_base_from_yaml(snapcraft_yaml) or "",
        cache_dir=Path(),  # not used in environment expansion
        arch=target_arch,
        parallel_build_count=parallel_build_count,
        project_name=snapcraft_yaml.get("name", ""),
        project_dirs=dirs,
        project_vars=project_vars,
        partitions=partitions,
    )
    set_global_environment(info)

    craft_parts.expand_environment(snapcraft_yaml, info=info, skip=["name", "version"])


def get_build_plan(
    yaml_data: dict[str, Any], parsed_args: "argparse.Namespace"
) -> list[tuple[str, str]]:
    """Get a list of all build_on->build_for architectures from the project file.

    Additionally, check for the command line argument `--build-for <architecture>`
    When defined, the build plan will only contain builds where `build-for`
    matches `SNAPCRAFT_BUILD_FOR`.
    Note: `--build-for` defaults to the environmental variable `SNAPCRAFT_BUILD_FOR`.

    :param yaml_data: The project YAML data.
    :param parsed_args: snapcraft's argument namespace

    :return: List of tuples of every valid build-on->build-for combination.
    """
    archs = models.ArchitectureProject.unmarshal(yaml_data).architectures

    host_arch = str(DebianArchitecture.from_host())
    build_plan: list[tuple[str, str]] = []

    # `isinstance()` calls are for mypy type checking and should not change logic
    for arch in [arch for arch in archs if isinstance(arch, models.Architecture)]:
        for build_on in arch.build_on:
            if build_on in host_arch and isinstance(arch.build_for, list):
                build_plan.append((host_arch, arch.build_for[0]))
            else:
                emit.verbose(
                    f"Skipping build-on: {build_on} build-for: {arch.build_for}"
                    f" because build-on doesn't match host arch: {host_arch}"
                )

    # filter out builds not matching argument `--build_for` or env `SNAPCRAFT_BUILD_FOR`
    build_for_arg = parsed_args.build_for
    if build_for_arg is not None:
        build_plan = [build for build in build_plan if build[1] == build_for_arg]

    if len(build_plan) == 0:
        emit.message(
            "Could not make build plan:"
            " build-on architectures in snapcraft.yaml"
            f" does not match host architecture ({host_arch})."
        )
    else:
        log_output = "Created build plan:"
        for build in build_plan:
            log_output += f"\n  build-on: {build[0]} build-for: {build[1]}"
        emit.trace(log_output)

    return build_plan


def _validate_and_get_partitions(yaml_data: dict[str, Any]) -> list[str] | None:
    """Validate partitions support, enable the feature, and get a list of partitions.

    :param yaml_data: The project's YAML data.

    :returns: A list of partitions containing the default partition and a partition for
    each component in the project or None if no components are defined.

    :raises SnapcraftError: If components are defined in the project but not supported.
    """
    project = models.ComponentProject.unmarshal(yaml_data)

    if project.components:
        if Features().enable_partitions:
            emit.debug("Not enabling partitions because feature is already enabled")
        else:
            emit.debug("Enabling partitions")
            Features.reset()
            Features(enable_partitions=True)

        return project.get_partitions()

    return None


def _is_manager(parsed_args: "argparse.Namespace") -> bool:
    """Check if snapcraft is managing build environments.

    :param parsed_args: The parsed arguments.

    :returns: True if this instance of snapcraft is managing a build environment.
    """
    return parsed_args.use_lxd or (
        not utils.is_managed_mode()
        and not parsed_args.destructive_mode
        and not os.getenv("SNAPCRAFT_BUILD_ENVIRONMENT") == "host"
    )


def _warn_on_multiple_builds(
    parsed_args: "argparse.Namespace", build_plan: list[tuple[str, str]]
) -> None:
    """Warn if snapcraft will build multiple snaps in the same environment.

    :param parsed_args: The parsed arguments.
    :param build_plan: The build plan.
    """
    # the only acceptable scenario for multiple items in the filtered build plan
    # is when snapcraft is managing build environments
    if not _is_manager(parsed_args) and len(build_plan) > 1:
        emit.message(
            "Warning: Snapcraft is building multiple snaps in the same "
            "environment which may result in unexpected behavior."
        )
        emit.message(
            "For more information, check out: "
            "https://snapcraft.io/docs/explanation-architectures#core22-8"
        )
