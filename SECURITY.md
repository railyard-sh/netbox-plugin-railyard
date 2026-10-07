<!--
TODO(owner): confirm before publishing.
- The reporting address below is the one railyard.sh's Security page publishes (support@railyard.sh).
  Swap in a dedicated security address if one is created, and keep this file and the Security page
  in step.
- Decide whether to also enable GitHub private vulnerability reporting for this repository, and
  mention it below if so.
-->

# Security policy

## Reporting a vulnerability

Please **do not open a public issue** for a security problem in this plugin.

Email **support@railyard.sh** with "netbox-plugin-railyard security" in the subject, and include:

- the plugin version (or commit) and your NetBox version;
- what an attacker could do, and the steps to reproduce it;
- any logs or proof of concept that help (remove secrets such as Railyard tokens first).

We will acknowledge your report and keep you updated while we investigate and fix it. Please give us
a reasonable opportunity to remediate before any public disclosure. While testing, do not access,
change or delete data that isn't yours, and don't degrade a service for others. We do not take legal
action against researchers who follow this guidance in good faith.

Vulnerabilities in Railyard itself (railyard.sh) are covered by the
[Railyard Security page](https://railyard.sh/security); the same address applies.

## Supported versions

The plugin is pre-1.0. Security fixes are made on `main` and in the latest release only; please
upgrade before reporting.

## Running the plugin safely

The README covers this in detail. In short: back up NetBox before syncing, dry-run first, give the
sync a dedicated, least-privilege Railyard token and rotate it before it expires, restrict which
Railyard organisations and projects can be synced (`railyard_allowed_orgs` /
`railyard_allowed_projects`), and grant the NetBox permissions to run a sync only to the people who
should change your DCIM data. The import writes estates to Railyard with the same token: give the token's user
the *editor* role only in the organisations you import into, and grant the import's permissions only to the
people who may send NetBox's data there.
