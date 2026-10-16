# Rollback

First inspect release history and the provider's genuine rollback mechanism. Identify the known-good release, its configuration, database compatibility, and current target. A previous release is not known-good solely because it exists. Run a rollback plan and explain possible downtime or data compatibility before seeking production authorization.

Use the package's `rollback` mode only when it reports the provider/resource eligible and the exact target is authorized. Some providers need redeployment or promotion rather than an atomic rollback; mark unsupported cases honestly. Afterward verify the actual application URL, key routes, HTTPS, and logs. A failed recovery remains FAILED or DEGRADED, with evidence.
