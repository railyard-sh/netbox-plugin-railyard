from netbox.plugins import PluginMenu, PluginMenuItem

from .policy import required_permissions

menu = PluginMenu(
    label="Railyard",
    icon_class="mdi mdi-sync",
    groups=(
        (
            "Sync",
            (
                PluginMenuItem(
                    link="plugins:netbox_railyard:sync",
                    link_text="Run sync",
                    # Hidden from users who couldn't even run a dry run.
                    permissions=sorted(required_permissions(dry_run=True, allow_deletes=False)),
                ),
            ),
        ),
    ),
)
