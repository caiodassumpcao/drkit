# Monitoring drkit with Prometheus

drkit writes its metrics in the Prometheus text format to a file that
[node_exporter](https://github.com/prometheus/node_exporter)'s **textfile collector** exposes
together with the host's own metrics. There is no daemon to run and no port to open: the
file is rewritten after every command that changes it, and Prometheus scrapes node_exporter as
usual.

## Setup

### 1. node_exporter

Enable the textfile collector with a directory for it (Debian and Ubuntu packages use
`/var/lib/prometheus/node-exporter`; the examples here use the upstream convention):

```bash
sudo install -d -m 0755 /var/lib/node_exporter/textfile_collector
# node_exporter flags:
#   --collector.textfile.directory=/var/lib/node_exporter/textfile_collector
```

node_exporter only needs to **read** the file: drkit (root) writes it with mode `0644`.

### 2. drkit

Point `metrics.textfile` at a `.prom` file in that directory (see
[configuration.md](configuration.md#metrics)):

```yaml
metrics:
  textfile: /var/lib/node_exporter/textfile_collector/drkit.prom
```

Then check and write it once:

```bash
sudo drkit doctor                 # "metrics": the directory exists and is writable
sudo drkit metrics --output /var/lib/node_exporter/textfile_collector/drkit.prom
curl -s localhost:9100/metrics | grep '^drkit_'
```

From then on, `drkit backup`, `verify`, `prune`, the three restores and `drill` rewrite the
file when they finish. The write is atomic (temporary file in the same directory, ignored by
the collector because its name does not end in `.prom`, then a rename), so node_exporter never
reads half a file. If the file cannot be written, drkit logs a warning
(`metrics.not_written`) and the command's result and exit code are unchanged.

`drkit metrics` (without `--output`) prints the same content on standard output. It reads
`state.json` only and never opens the repository.

## Metrics

Every sample has a `host` label (the manifest's `host`). All metrics are gauges; times are
Unix timestamps in seconds, durations in seconds, sizes in bytes. **A metric without data is
not exported** (no backup yet, no restore of that kind, a state file written by 1.0): alert on
the age of what exists, and on its absence if needed (`absent(...)`), never on a zero that
means "unknown".

| Metric | Labels | Meaning |
|---|---|---|
| `drkit_info` | `version` | Always `1`: the drkit version that wrote the file |
| `drkit_last_run_timestamp_seconds` | | End of the last backup run |
| `drkit_last_success_timestamp_seconds` | | End of the last backup run in which every item succeeded (the RPO is `time()` minus this) |
| `drkit_last_run_duration_seconds` | | Duration of the last backup run |
| `drkit_last_run_success` | | `1` if every item of the last run succeeded, else `0` |
| `drkit_last_run_items` | `status` (`ok`, `failed`) | Items of the last run by outcome |
| `drkit_last_run_bytes_added` | | New data the last run added to the repository, after deduplication |
| `drkit_project_last_success_timestamp_seconds` | `project` | End of the last run in which every item of the project succeeded |
| `drkit_last_verify_timestamp_seconds` | | End of the last `drkit verify` |
| `drkit_last_verify_success` | | `1` if it passed |
| `drkit_last_prune_timestamp_seconds` | | End of the last `drkit prune` |
| `drkit_last_prune_success` | | `1` if it succeeded |
| `drkit_last_restore_timestamp_seconds` | `kind` (`db`, `project`, `host`) | End of the last restore of that kind |
| `drkit_last_restore_success` | `kind` | `1` if it succeeded |
| `drkit_last_restore_rto_seconds` | `kind` | Recovery time of the last successful restore: host, start to the last green healthcheck; db and project, duration |
| `drkit_last_drill_timestamp_seconds` | | End of the last `drkit drill` |
| `drkit_last_drill_success` | | `1` if it passed |
| `drkit_last_drill_rto_seconds` | | RTO measured by the last drill |
| `drkit_last_drill_rpo_seconds` | | RPO measured by the last drill |

The drill runs from its own manifest; it updates the host's file only if the host has a
manifest (`/etc/drkit/dr.yaml` or `--config`) with `metrics.textfile`.

## Alerting rules

[`examples/prometheus/drkit.rules.yml`](../examples/prometheus/drkit.rules.yml) is a ready-made
rule group:

| Alert | Fires when | Severity |
|---|---|---|
| `DrkitBackupStale` | no successful backup for 2 hours (5 minutes) | critical |
| `DrkitBackupFailed` | the last backup run had failed items | warning |
| `DrkitVerifyFailed` | the last verify failed | critical |
| `DrkitVerifyStale` | no verify for 2 days (5 minutes) | warning |
| `DrkitPruneFailed` | the last prune failed | warning |
| `DrkitPruneStale` | no prune for 2 weeks (5 minutes) | warning |
| `DrkitRestoreFailed` | the last restore of a kind failed | critical |
| `DrkitMetricsStale` | node_exporter sees `drkit.prom` unchanged for 2 hours (5 minutes) | warning |

Each "stale" threshold is **twice the interval of the matching timer** of
[`examples/systemd`](../examples/systemd) (backup hourly: `2 * 3600`; verify daily; prune
weekly). Change them with your timers, e.g. `2 * 6 * 3600` for a backup every 6 hours.
`DrkitMetricsStale` relies on node_exporter's `node_textfile_mtime_seconds`; since drkit
rewrites the file after every backup, its threshold follows the backup interval too.

Load the rules in Prometheus:

```yaml
rule_files:
  - /etc/prometheus/rules/drkit.rules.yml
```

## Testing

With [promtool](https://prometheus.io/docs/prometheus/latest/command-line/promtool/) (shipped
with Prometheus):

```bash
drkit metrics | promtool check metrics                  # format and naming lint
promtool check rules examples/prometheus/drkit.rules.yml
cd examples/prometheus && promtool test rules drkit.rules.test.yml
```

`drkit.rules.test.yml` covers every alert: a healthy host where nothing fires, stopped backups
(and a stale file), failures, and verify and prune that stopped running. CI runs all three
commands with a pinned promtool on every pull request (`tests/unit/test_promtool.py`); after
changing a threshold, update the matching test.
