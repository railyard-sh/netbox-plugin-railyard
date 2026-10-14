"""The NetBox-free suite: the plugin's policy decisions, unit-tested without NetBox. The Railyard core
(client, sync-document adapter, importer, ownership tag) is railyard-sync's and is tested there."""

# tests/integration needs a live NetBox (it runs under NetBox's own test runner; see dev/run-integration-tests.sh).
collect_ignore = ["integration"]
