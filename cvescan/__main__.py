#!/usr/bin/env python3

import argparse as ap
import json
import logging
import os
import sys

from tabulate import tabulate

import cvescan.constants as const
import cvescan.downloader as downloader
import cvescan.manifest_parser as manifest_parser
from cvescan.cvescanner import CVEScanner
from cvescan.errors import ArgumentError, DistribIDError, PkgCountError
from cvescan.options import Options
from cvescan.output_formatters import (
    CLIOutputFormatter,
    CVEOutputFormatter,
    CVEScanResultSorter,
    NagiosOutputFormatter,
    PackageScanResultSorter,
)
from cvescan.sysinfo import SysInfo


def set_output_verbosity(args):
    if args.silent:
        return get_null_logger()

    logger = logging.getLogger("cvescan.stdout")

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    log_formatter = logging.Formatter("%(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    logger.addHandler(stream_handler)

    return logger


def get_null_logger():
    logger = logging.getLogger("cvescan.null")
    if not logger.hasHandlers():
        logger.addHandler(logging.NullHandler())

    return logger


LOGGER = get_null_logger()


def error_exit(msg, code=const.ERROR_RETURN_CODE):
    print("Error: %s" % msg, file=sys.stderr)
    sys.exit(code)


def parse_args():
    cvescan_ap = ap.ArgumentParser(
        description=const.CVESCAN_DESCRIPTION, formatter_class=ap.RawTextHelpFormatter
    )
    cvescan_ap.add_argument(
        "-c", "--cve", metavar="CVE-IDENTIFIER", help=const.CVE_HELP
    )
    cvescan_ap.add_argument(
        "-p",
        "--priority",
        help=const.PRIORITY_HELP,
        choices=[const.CRITICAL, const.HIGH, const.MEDIUM, const.ALL],
        default="high",
    )
    cvescan_ap.add_argument(
        "-s", "--silent", action="store_true", default=False, help=const.SILENT_HELP
    )
    cvescan_ap.add_argument("-o", "--oval-file", help=const.OVAL_FILE_HELP)
    cvescan_ap.add_argument("-m", "--manifest-file", help=const.MANIFEST_HELP)
    cvescan_ap.add_argument(
        "-n", "--nagios", action="store_true", default=False, help=const.NAGIOS_HELP
    )
    cvescan_ap.add_argument(
        "--uct-links", action="store_true", default=False, help=const.UCT_LINKS_HELP
    )
    cvescan_ap.add_argument(
        "--unresolved", action="store_true", default=False, help=const.UNRESOLVED_HELP
    )
    cvescan_ap.add_argument(
        "-v", "--verbose", action="store_true", default=False, help=const.VERBOSE_HELP
    )
    cvescan_ap.add_argument(
        "-x",
        "--experimental",
        action="store_true",
        default=False,
        help=const.EXPERIMENTAL_HELP,
    )

    return cvescan_ap.parse_args()


def log_config_options(opt):
    LOGGER.debug("Config Options")
    table = [
        ["Manifest Mode", opt.manifest_mode],
        ["Experimental Mode", opt.experimental_mode],
        ["Nagios Output Mode", opt.nagios_mode],
        ["OVAL File Path", opt.oval_file],
        ["OVAL URL", opt.oval_base_url],
        ["Manifest File", opt.manifest_file],
        ["Check Specific CVE", opt.cve],
        ["CVE Priority", opt.priority],
        ["Show Unresolved CVEs", opt.unresolved],
    ]

    LOGGER.debug(tabulate(table))
    LOGGER.debug("")


def log_system_info(sysinfo, manifest_mode):
    LOGGER.debug("System Info")
    table = [
        ["CVEScan is a Snap", sysinfo.is_snap],
        ["$SNAP_USER_COMMON", sysinfo.snap_user_common],
    ]

    if not manifest_mode:
        table = [
            ["Local Ubuntu Codename", sysinfo.distrib_codename],
            ["Installed Package Count", sysinfo.package_count],
            ["ESM Apps Enabled", sysinfo.esm_apps_enabled],
            ["ESM Infra Enabled", sysinfo.esm_infra_enabled],
        ] + table

    LOGGER.debug(tabulate(table))
    LOGGER.debug("")


def load_output_formatter(opt, sysinfo):
    if opt.cve:
        return CVEOutputFormatter(opt, sysinfo, LOGGER)

    sorter = load_output_sorter(opt, sysinfo)
    if opt.nagios_mode:
        return NagiosOutputFormatter(opt, sysinfo, LOGGER, sorter=sorter)

    return CLIOutputFormatter(opt, sysinfo, LOGGER, sorter=sorter)


def load_output_sorter(opt, sysinfo):
    pkg_sorter = PackageScanResultSorter()
    return CVEScanResultSorter(subsorters=[pkg_sorter])


def load_uct_data(opt):
    if opt.download_oval_file:
        downloader.download_bz2_file(LOGGER, opt.base_url, opt.oval_zip, opt.oval_file)

    with open(opt.oval_file) as oval_file:
        uct_data = json.load(oval_file)

    return uct_data


def get_installed_pkgs_and_codename(sysinfo, manifest_file):
    if manifest_file:
        (installed_pkgs, codename) = manifest_parser.parse_manifest_file(manifest_file)
    else:
        installed_pkgs = sysinfo.installed_packages
        codename = sysinfo.distrib_codename

    LOGGER.debug("Target system code name is %s." % codename)
    LOGGER.debug("Target system has %d packages installed." % len(installed_pkgs))

    return (installed_pkgs, codename)


def main():
    global LOGGER

    args = parse_args()

    # Configure debug logging as early as possible
    LOGGER = set_output_verbosity(args)

    sysinfo = SysInfo(LOGGER)

    try:
        opt = Options(args)
    except (ArgumentError, ValueError) as err:
        error_exit("Invalid option or argument: %s" % err, const.CLI_ERROR_RETURN_CODE)

    error_exit_code = (
        const.NAGIOS_UNKNOWN_RETURN_CODE if opt.nagios_mode else const.ERROR_RETURN_CODE
    )

    try:
        installed_pkgs, codename = get_installed_pkgs_and_codename(
            sysinfo, opt.manifest_file
        )

        log_config_options(opt)
        log_system_info(sysinfo, opt.manifest_mode)
    except (FileNotFoundError, PermissionError) as err:
        error_exit("Failed to determine the correct Ubuntu codename: %s" % err)
    except DistribIDError as di:
        error_exit(
            "Invalid linux distribution detected, CVEScan must be run on Ubuntu: %s"
            % di
        )
    except PkgCountError as pke:
        error_exit("Failed to determine the local package count: %s" % pke)

    output_formatter = load_output_formatter(opt, sysinfo)

    if sysinfo.is_snap:
        LOGGER.debug(
            "Running as a snap, changing to '%s' directory." % sysinfo.snap_user_common
        )
        LOGGER.debug(
            "Downloaded files, log files and temporary reports will "
            "be in '%s'" % sysinfo.snap_user_common
        )

        try:
            os.chdir(sysinfo.snap_user_common)
        except Exception:
            error_exit("failed to cd to %s" % sysinfo.snap_user_common, error_exit_code)

    try:
        uct_data = load_uct_data(opt)
        cve_scanner = CVEScanner(LOGGER)
        scan_results = cve_scanner.scan(codename, uct_data, installed_pkgs)
        (results, return_code) = output_formatter.format_output(scan_results)
    except Exception as ex:
        error_exit(
            "An unexpected error occurred while running CVEScan: %s" % ex,
            error_exit_code,
        )

    LOGGER.info(results)
    sys.exit(return_code)


if __name__ == "__main__":
    main()
