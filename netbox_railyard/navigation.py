from netbox.plugins import PluginMenu, PluginMenuItem

from .policy import import_permissions, required_permissions

menu = PluginMenu(
    label="Railyard",
    icon_class="mdi mdi-sync",
    groups=(
        (
            "Railyard",
            (
                PluginMenuItem(
                    link="plugins:netbox_railyard:sync",
                    link_text="Sync from Railyard",
                    # Hidden from users who couldn't even run a dry run.
                    permissions=sorted(required_permissions(dry_run=True, allow_deletes=False)),
                ),
                PluginMenuItem(
                    link="plugins:netbox_railyard:import",
                    link_text="Import into Railyard",
                    permissions=sorted(import_permissions()),
                ),
            ),
        ),
    ),
)
