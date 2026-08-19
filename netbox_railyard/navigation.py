from netbox.plugins import PluginMenu, PluginMenuItem

menu = PluginMenu(
    label="Railyard",
    icon_class="mdi mdi-sync",
    groups=(
        (
            "Sync",
            (PluginMenuItem(link="plugins:netbox_railyard:sync", link_text="Run sync"),),
        ),
    ),
)
