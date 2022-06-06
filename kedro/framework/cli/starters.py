"""kedro is a CLI for managing Kedro projects.

This module implements commands available from the kedro CLI for creating
projects.
"""
import os
import re
import shutil
import stat
import tempfile
from collections import OrderedDict
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from warnings import warn

import click
import pkg_resources
import yaml

import kedro
from kedro import __version__ as version
from kedro.framework.cli.utils import (
    CONTEXT_SETTINGS,
    ENTRY_POINT_GROUPS,
    KedroCliError,
    _clean_pycache,
    _filter_deprecation_warnings,
    command_with_verbosity,
)

KEDRO_PATH = Path(kedro.__file__).parent
TEMPLATE_PATH = KEDRO_PATH / "templates" / "project"

_STARTER_ALIASES = {
    "astro-airflow-iris",
    "standalone-datacatalog",
    "pandas-iris",
    "pyspark",
    "pyspark-iris",
    "spaceflights",
}
_STARTERS_REPO = "git+https://github.com/kedro-org/kedro-starters.git"

# The `astro-iris` was renamed to `astro-airflow-iris`, but old (external) documentation
# and tutorials still refer to `astro-iris`. We create an alias to check if a user has
# entered old `astro-iris` as the starter name and changes it to `astro-airflow-iris`.
_STARTERS_ALIASES = [
    {
        "name": "astro-airflow-iris",
        "template_path": _STARTERS_REPO,
        "directory": "astro-airflow-iris",
    },
    # this is an alias name for "astro-airflow-iris"
    {
        "name": "astro-iris",
        "template_path": _STARTERS_REPO,
        "directory": "astro-airflow-iris",
    },
    {"name": "mini-kedro", "template_path": _STARTERS_REPO, "directory": "mini-kedro"},
    {
        "name": "pandas-iris",
        "template_path": _STARTERS_REPO,
        "directory": "pandas-iris",
    },
    {"name": "pyspark", "template_path": _STARTERS_REPO, "directory": "pyspark"},
    {
        "name": "pyspark-iris",
        "template_path": _STARTERS_REPO,
        "directory": "pyspark-iris",
    },
    {
        "name": "spaceflights",
        "template_path": _STARTERS_REPO,
        "directory": "spaceflights",
    },
]


CONFIG_ARG_HELP = """Non-interactive mode, using a configuration yaml file. This file
must supply  the keys required by the template's prompts.yml. When not using a starter,
these are `project_name`, `repo_name` and `python_package`."""
STARTER_ARG_HELP = """Specify the starter template to use when creating the project.
This can be the path to a local directory, a URL to a remote VCS repository supported
by `cookiecutter` or one of the aliases listed in ``kedro starter list``.
"""
CHECKOUT_ARG_HELP = (
    "An optional tag, branch or commit to checkout in the starter repository."
)
DIRECTORY_ARG_HELP = (
    "An optional directory inside the repository where the starter resides."
)


# pylint: disable=unused-argument
def _remove_readonly(func: Callable, path: Path, excinfo: Tuple):  # pragma: no cover
    """Remove readonly files on Windows
    See: https://docs.python.org/3/library/shutil.html?highlight=shutil#rmtree-example
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _check_starter_entrypoint_config(module_name: str, config: Dict[str, str]) -> bool:
    flag_config_ok = True
    if not isinstance(config, dict):
        warn(
            f"The starter configuration loaded from module {module_name} should be a 'dict', got '{type(config)}' instead"
        )
        flag_config_ok = False
    mandatory_keys = {"name", "template_path"}
    optional_keys = {"directory"}
    provided_keys = set(config.keys())

    missing_keys = mandatory_keys - provided_keys
    if missing_keys:  # mandatory keys
        warn(
            f"Entrypoint kedro.starters from {module_name} must have the following keys, which are currently missing: '{missing_keys}'"
        )
        flag_config_ok = False

    extra_keys = provided_keys - mandatory_keys - optional_keys  # optional keys
    if extra_keys:  # mandatory keys
        warn(
            f"Entrypoint kedro.starters from {module_name} has keys '{extra_keys}' which are not allowed"
        )
        flag_config_ok = False

    return flag_config_ok


def _get_starters_aliases() -> List[Dict[str, str]]:
    """This functions lists all the starters aliases declared in
    the core repo and in plugins entry points.
    The output looks like:
    [
        {"name": "astro-airflow-iris", "template_path": ..., "directory": ..., "origin": "kedro"},
        ...,
        {"name": "my-awesome-starter", "template_path": ..., "directory": ..., "origin": "my-awesome-plugin"}
    ]
    """

    # add an extra key to indicate from where the plugin come from
    starters_aliases = [{**config, "origin": "kedro"} for config in _STARTERS_ALIASES]

    existing_names: Dict[str, str] = {}  # dict {name: module_name}
    for starter_entry_point in pkg_resources.iter_entry_points(
        group=ENTRY_POINT_GROUPS["starters"]
    ):
        module_name = starter_entry_point.module_name.split(".")[0]
        for starter_config in starter_entry_point.load():
            config_status = _check_starter_entrypoint_config(
                module_name, starter_config
            )
            if config_status is False:
                click.secho(
                    f"Starter alias `{starter_config['name']}` from `{module_name}` has been ignored as the config is invalid and cannot be loaded",
                    fg="yellow",
                )
            elif starter_config["name"] in existing_names:
                click.secho(
                    f"Starter alias `{starter_config['name']}` from `{module_name}` has been ignored as it is already defined by `{existing_names[starter_config['name']]}`",
                    fg="yellow",
                )
            else:
                starters_aliases.append({**starter_config, "origin": module_name})
                existing_names[starter_config["name"]] = module_name

    return starters_aliases


# pylint: disable=missing-function-docstring
@click.group(context_settings=CONTEXT_SETTINGS, name="Kedro")
def create_cli():  # pragma: no cover
    pass


@command_with_verbosity(create_cli, short_help="Create a new kedro project.")
@click.option(
    "--config",
    "-c",
    "config_path",
    type=click.Path(exists=True),
    help=CONFIG_ARG_HELP,
)
@click.option("--starter", "-s", "starter_name", help=STARTER_ARG_HELP)
@click.option("--checkout", help=CHECKOUT_ARG_HELP)
@click.option("--directory", help=DIRECTORY_ARG_HELP)
def new(
    config_path, starter_name, checkout, directory, **kwargs
):  # pylint: disable=unused-argument
    """Create a new kedro project."""
    if checkout and not starter_name:
        raise KedroCliError("Cannot use the --checkout flag without a --starter value.")

    if directory and not starter_name:
        raise KedroCliError(
            "Cannot use the --directory flag without a --starter value."
        )

    starters_aliases = _get_starters_aliases()

    # see https://www.geeksforgeeks.org/group-list-of-dictionary-data-by-particular-key-in-python/
    # this returns a nested dictionary {name1: {template_path: xxx, directory: xxx}, name2: {template_path: xxx, directory: xxx}, ...}
    # and we know that each starter has only one config, so we can convert take the first item of the list
    starters_aliases_by_name = {
        name: list(config)[0]
        for name, config in groupby(starters_aliases, key=itemgetter("name"))
    }

    if starter_name in starters_aliases_by_name:
        if directory:
            raise KedroCliError(
                "Cannot use the --directory flag with a --starter alias."
            )
        template_path = starters_aliases_by_name[starter_name]["template_path"]
        # "directory" is an optional key for starters from plugins, so if the key is not present we will use "None".
        directory = starters_aliases_by_name[starter_name].get("directory")
        checkout = checkout or version
    elif starter_name is not None:
        template_path = starter_name
        checkout = checkout or version
    else:
        template_path = str(TEMPLATE_PATH)

    # Get prompts.yml to find what information the user needs to supply as config.
    tmpdir = tempfile.mkdtemp()
    cookiecutter_dir = _get_cookiecutter_dir(template_path, checkout, directory, tmpdir)
    prompts_required = _get_prompts_required(cookiecutter_dir)
    # We only need to make cookiecutter_context if interactive prompts are needed.
    if not config_path:
        cookiecutter_context = _make_cookiecutter_context_for_prompts(cookiecutter_dir)

    # Cleanup the tmpdir after it's no longer required.
    # Ideally we would want to be able to use tempfile.TemporaryDirectory() context manager
    # but it causes an issue with readonly files on windows
    # see: https://bugs.python.org/issue26660.
    # So onerror, we will attempt to clear the readonly bits and re-attempt the cleanup
    shutil.rmtree(tmpdir, onerror=_remove_readonly)

    # Obtain config, either from a file or from interactive user prompts.
    if not prompts_required:
        config = {}
        if config_path:
            config = _fetch_config_from_file(config_path)
    elif config_path:
        config = _fetch_config_from_file(config_path)
        _validate_config_file(config, prompts_required)
    else:
        config = _fetch_config_from_user_prompts(prompts_required, cookiecutter_context)

    cookiecutter_args = _make_cookiecutter_args(config, checkout, directory)
    _create_project(template_path, cookiecutter_args)


@create_cli.group()
def starter():
    """Commands for working with project starters."""


@starter.command("list")
def list_starters():
    """List all official project starters available."""
    starters_aliases = _get_starters_aliases()
    starters_aliases_by_origin = {
        origin: list(starter_config)
        for origin, starter_config in groupby(
            sorted(starters_aliases, key=itemgetter("origin")), key=itemgetter("origin")
        )
    }

    # ensure kedro starters are listed first
    built_in_config = starters_aliases_by_origin.pop("kedro")
    starters_aliases_by_origin = {
        "kedro": built_in_config,
        **starters_aliases_by_origin,
    }

    for origin, module_starters_config in starters_aliases_by_origin.items():
        click.secho(f"\nStarters from {origin}\n", fg="yellow")
        for starter_config in module_starters_config:
            del starter_config["origin"]
            name = starter_config.pop("name")
            click.echo(yaml.dump({name: starter_config}))


def _fetch_config_from_file(config_path: str) -> Dict[str, str]:
    """Obtains configuration for a new kedro project non-interactively from a file.

    Args:
        config_path: The path of the config.yml which should contain the data required
            by ``prompts.yml``.

    Returns:
        Configuration for starting a new project. This is passed as ``extra_context``
            to cookiecutter and will overwrite the cookiecutter.json defaults.

    Raises:
        KedroCliError: If the file cannot be parsed.

    """
    try:
        with open(config_path, encoding="utf-8") as config_file:
            config = yaml.safe_load(config_file)

        if KedroCliError.VERBOSE_ERROR:
            click.echo(config_path + ":")
            click.echo(yaml.dump(config, default_flow_style=False))
    except Exception as exc:
        raise KedroCliError(
            f"Failed to generate project: could not load config at {config_path}."
        ) from exc

    return config


def _make_cookiecutter_args(
    config: Dict[str, str],
    checkout: str,
    directory: str,
) -> Dict[str, Any]:
    """Creates a dictionary of arguments to pass to cookiecutter.

    Args:
        config: Configuration for starting a new project. This is passed as
            ``extra_context`` to cookiecutter and will overwrite the cookiecutter.json
            defaults.
        checkout: The tag, branch or commit in the starter repository to checkout.
            Maps directly to cookiecutter's ``checkout`` argument. Relevant only when
            using a starter.
        directory: The directory of a specific starter inside a repository containing
            multiple starters. Maps directly to cookiecutter's ``directory`` argument.
            Relevant only when using a starter.
            https://cookiecutter.readthedocs.io/en/1.7.2/advanced/directories.html

    Returns:
        Arguments to pass to cookiecutter.
    """
    config.setdefault("kedro_version", version)

    cookiecutter_args = {
        "output_dir": config.get("output_dir", str(Path.cwd().resolve())),
        "no_input": True,
        "extra_context": config,
    }
    if checkout:
        cookiecutter_args["checkout"] = checkout
    if directory:
        cookiecutter_args["directory"] = directory

    return cookiecutter_args


def _create_project(template_path: str, cookiecutter_args: Dict[str, str]):
    """Creates a new kedro project using cookiecutter.

    Args:
        template_path: The path to the cookiecutter template to create the project.
            It could either be a local directory or a remote VCS repository
            supported by cookiecutter. For more details, please see:
            https://cookiecutter.readthedocs.io/en/latest/usage.html#generate-your-project
        cookiecutter_args: Arguments to pass to cookiecutter.

    Raises:
        KedroCliError: If it fails to generate a project.
    """
    with _filter_deprecation_warnings():
        # pylint: disable=import-outside-toplevel
        from cookiecutter.main import cookiecutter  # for performance reasons

    try:
        result_path = cookiecutter(template=template_path, **cookiecutter_args)
    except Exception as exc:
        raise KedroCliError(
            "Failed to generate project when running cookiecutter."
        ) from exc

    _clean_pycache(Path(result_path))
    click.secho(
        f"\nChange directory to the project generated in {result_path}",
        fg="green",
    )
    click.secho(
        "\nA best-practice setup includes initialising git and creating "
        "a virtual environment before running ``pip install -r src/requirements.txt`` to install "
        "project-specific dependencies. Refer to the Kedro documentation: "
        "https://kedro.readthedocs.io/"
    )


def _get_cookiecutter_dir(
    template_path: str, checkout: str, directory: str, tmpdir: str
) -> Path:
    """Gives a path to the cookiecutter directory. If template_path is a repo then
    clones it to ``tmpdir``; if template_path is a file path then directly uses that
    path without copying anything.
    """
    # pylint: disable=import-outside-toplevel
    from cookiecutter.exceptions import RepositoryCloneFailed, RepositoryNotFound
    from cookiecutter.repository import determine_repo_dir  # for performance reasons

    try:
        cookiecutter_dir, _ = determine_repo_dir(
            template=template_path,
            abbreviations={},
            clone_to_dir=Path(tmpdir).resolve(),
            checkout=checkout,
            no_input=True,
            directory=directory,
        )
    except (RepositoryNotFound, RepositoryCloneFailed) as exc:
        error_message = f"Kedro project template not found at {template_path}."

        if checkout:
            error_message += (
                f" Specified tag {checkout}. The following tags are available: "
                + ", ".join(_get_available_tags(template_path))
            )
        official_starters = sorted(_STARTERS_ALIASES)
        raise KedroCliError(
            f"{error_message}. The aliases for the official Kedro starters are: \n"
            f"{yaml.safe_dump(official_starters)}"
        ) from exc

    return Path(cookiecutter_dir)


def _get_prompts_required(cookiecutter_dir: Path) -> Optional[Dict[str, Any]]:
    """Finds the information a user must supply according to prompts.yml."""
    prompts_yml = cookiecutter_dir / "prompts.yml"
    if not prompts_yml.is_file():
        return None

    try:
        with prompts_yml.open("r") as prompts_file:
            return yaml.safe_load(prompts_file)
    except Exception as exc:
        raise KedroCliError(
            "Failed to generate project: could not load prompts.yml."
        ) from exc


def _fetch_config_from_user_prompts(
    prompts: Dict[str, Any], cookiecutter_context: OrderedDict
) -> Dict[str, str]:
    """Interactively obtains information from user prompts.

    Args:
        prompts: Prompts from prompts.yml.
        cookiecutter_context: Cookiecutter context generated from cookiecutter.json.

    Returns:
        Configuration for starting a new project. This is passed as ``extra_context``
            to cookiecutter and will overwrite the cookiecutter.json defaults.
    """
    # pylint: disable=import-outside-toplevel
    from cookiecutter.environment import StrictEnvironment
    from cookiecutter.prompt import read_user_variable, render_variable

    config: Dict[str, str] = {}

    for variable_name, prompt_dict in prompts.items():
        prompt = _Prompt(**prompt_dict)

        # render the variable on the command line
        cookiecutter_variable = render_variable(
            env=StrictEnvironment(context=cookiecutter_context),
            raw=cookiecutter_context[variable_name],
            cookiecutter_dict=config,
        )

        # read the user's input for the variable
        user_input = read_user_variable(str(prompt), cookiecutter_variable)
        if user_input:
            prompt.validate(user_input)
            config[variable_name] = user_input
    return config


def _make_cookiecutter_context_for_prompts(cookiecutter_dir: Path):
    # pylint: disable=import-outside-toplevel
    from cookiecutter.generate import generate_context

    cookiecutter_context = generate_context(cookiecutter_dir / "cookiecutter.json")
    return cookiecutter_context.get("cookiecutter", {})


class _Prompt:
    """Represent a single CLI prompt for `kedro new`"""

    def __init__(self, *args, **kwargs) -> None:  # pylint: disable=unused-argument
        try:
            self.title = kwargs["title"]
        except KeyError as exc:
            raise KedroCliError(
                "Each prompt must have a title field to be valid."
            ) from exc

        self.text = kwargs.get("text", "")
        self.regexp = kwargs.get("regex_validator", None)
        self.error_message = kwargs.get("error_message", "")

    def __str__(self) -> str:
        title = self.title.strip().title()
        title = click.style(title + "\n" + "=" * len(title), bold=True)
        prompt_lines = [title] + [self.text]
        prompt_text = "\n".join(str(line).strip() for line in prompt_lines)
        return f"\n{prompt_text}\n"

    def validate(self, user_input: str) -> None:
        """Validate a given prompt value against the regex validator"""
        if self.regexp and not re.match(self.regexp, user_input):
            click.secho(f"`{user_input}` is an invalid value.", fg="red", err=True)
            click.secho(self.error_message, fg="red", err=True)
            raise ValueError(user_input)


def _get_available_tags(template_path: str) -> List:
    # Not at top level so that kedro CLI works without a working git executable.
    # pylint: disable=import-outside-toplevel
    import git

    try:
        tags = git.cmd.Git().ls_remote("--tags", template_path.replace("git+", ""))

        unique_tags = {
            tag.split("/")[-1].replace("^{}", "") for tag in tags.split("\n")
        }
        # Remove git ref "^{}" and duplicates. For example,
        # tags: ['/tags/version', '/tags/version^{}']
        # unique_tags: {'version'}

    except git.GitCommandError:
        return []
    return sorted(unique_tags)


def _validate_config_file(config: Dict[str, str], prompts: Dict[str, Any]):
    """Checks that the configuration file contains all needed variables.

    Args:
        config: The config as a dictionary.
        prompts: Prompts from prompts.yml.

    Raises:
        KedroCliError: If the config file is empty or does not contain all the keys
            required in prompts, or if the output_dir specified does not exist.
    """
    if config is None:
        raise KedroCliError("Config file is empty.")
    missing_keys = set(prompts) - set(config)
    if missing_keys:
        click.echo(yaml.dump(config, default_flow_style=False))
        raise KedroCliError(f"{', '.join(missing_keys)} not found in config file.")

    if "output_dir" in config and not Path(config["output_dir"]).exists():
        raise KedroCliError(
            f"`{config['output_dir']}` is not a valid output directory. "
            "It must be a relative or absolute path to an existing directory."
        )
