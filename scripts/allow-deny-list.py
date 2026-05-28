#!/bin/env python3
"""
This script lists the commands that are allowed and denied by the OpenStack CLI.

It outputs to stdout a YAML dictionary with the following keys:
- allow_commands: commands that are allowed by the ACCEPT set
- deny_commands: commands that are denied by the REJECT set
- ignore_commands: commands that are just artifacts of the arg parsing mechanism
- undefined_commands: commands that are not in any other group. When not empty,
                      it means that the script needs to be updated.

The purpose is to verify that when we update the openstack client library we can
do a diff of the allowed commands.

The accept commands list is generated using the rhros_ls_mcps package itself,
where the reject and ignore list of commands come from this script.
"""

from importlib.metadata import entry_points

import sys
import yaml

from rhos_ls_mcps import osc


# TODO: Revisit following verbs:
# - save: Maybe we can make the resources downloadable using MCP resources?
# - export:
# - cp:
REJECT_COMMANDS: set[str] = {
    "create",
    "delete",
    "update",
    "set",
    "unset",
    "remove",
    "add",
    "abort",
    "complete",
    "revoke",
    "issue",
    "cleanup",
    "migrate",
    "resize",
    "cleanup",
    "shelve",
    "unshelve",
    "reboot",
    "restart",
    "rebuild",
    "stop",
    "restore",
    "import",
    "failover",
    "associate",
    "revert",
    "run",
    "save",
    "shrink",
    "reset",
    "del",
    "onboard",
    "commit",
    "unrescue",
    "adopt",
    "on",
    "off",
    "forcedown",
    "detach",
    "edit",
    "lock",
    "unlock",
    "purge",
    "rerun",
    "attach",
    "resume",
    "start",
    "pause",
    "create-from-file",
    "request-refresh",
    "rename",
    "post",
    "clear",
    "move",
    "manage",
    "enable",
    "register",
    "rescue",
    "deploy",
    "unpause",
    "disable",
    "benchmark metric create",
    "abandon",
    "renew",
    "ssh",
    "export",
    "replace",
    "alarm create",
    "alarm update",
    "alarm quota set",
    "alarm state set",
    "recover",
    "cancel",
    "unhold",
    "accept",
    "pull",
    "exec",
    "upgrade",
    "suspend",
    "disassociate",
    "undeploy",
    "grow",
    "scale",
    "execute",
    "grant",
    "confirm",
    "kill",
    "mark",
    "eject",
    "op",
    "verification",
    "reprocess",
    "expand",
    "evacuate",
    "signed",
    "axfr",
    "unregister",
    "clean",
    "download",
    "authorize",
    "cp",
    "submit",
    "stage",
    "promote",
    "configure",
    "inject",
    "signal",
    "release",
    # These are full names
    "secret_store",
    "baremetal_node_inspect",
    "baremetal_node_service",
    "baremetal_node_provide",
    "aggregate_cache_image",
    "alarm delete",
    "cached_image_queue",
    "baremetal_driver_passthru_call",
    "baremetal_node_passthru_call",
    "static-action_call",
    "metric_benchmark measures add",
    "metric_measures_batch-metrics",
    "metric_measures_batch-resources-metrics",
    # This sounds intrusive: https://docs.openstack.org/senlin/rocky/user/nodes.html#checking-a-node
    "cluster_node_check",
}

# These must be full names with the "_" suffix, and they are not really commands but artifacts
# from the arg parsing mechanism
IGNORE_COMMANDS: set[str] = {
    "database_",
    "infra_optim_",
    "load_balancer_",
    "identity_",
    "neutronclient_",
    "rca_",
    "object_store_",
    "compute_",
    "container_",
    "dns_",
    "key_manager_",
    "application_catalog_",
    "congressclient_",
    "messaging_",
    "baremetal_",
    "image_",
    "volume_",
    "network_",
    "clustering_",
    "metric_",
    "baremetal-introspection_",
    "cluster_profile_type_ops_",
    "workflow_engine_",
    "data_processing_",
    "orchestration_",
}


def osp_list_commands(verbs: set[str]) -> tuple[list[str], list[str]]:
    """List commands that match and don't match the given verbs"""
    # Use sets because some commands exist in multiple groups
    # eg: dataprocessing_cluster_show exists in openstack.data_processing.v1 and
    # openstack.data_processing.v2
    result_commands: set[str] = set[str]()
    result_other_commands: set[str] = set[str]()

    all_entry_points = entry_points()
    for group in all_entry_points.groups:
        if group.startswith("openstack."):
            for ep in all_entry_points.select(group=group):
                name = ep.name
                cmd = name.split("_")
                if verbs.intersection(cmd) or name in verbs:
                    result_commands.add(name + "_")
                else:
                    result_other_commands.add(name + "_")
    return list(result_commands), list(result_other_commands)


def get_openstackclient_version() -> str | None:
    import openstackclient

    return openstackclient.__version__


def main() -> None:
    accept_commands, non_accept_commands = osc.osp_list_commands(osc.ACCEPT_COMMANDS)
    reject_commands, non_reject_commands = osc.osp_list_commands(REJECT_COMMANDS)

    undefined_commands: list[str] = list(
        set(non_accept_commands).intersection(non_reject_commands) - IGNORE_COMMANDS
    )

    result = {
        "undefined_commands": undefined_commands,
        "deny_commands": reject_commands,
        "allow_commands": accept_commands,
        "ignore_commands": list(IGNORE_COMMANDS),
        "python_osc_version": get_openstackclient_version(),
    }
    yaml.dump(result, sys.stdout)


if __name__ == "__main__":
    main()
