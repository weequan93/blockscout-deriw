# DeriW devnet

Use this directory for devnet's own `.env` and `envs/*.env` files. Setup and
startup commands are in [the environment-directory guide](../deploy/compose/README.md).
The original `docker-compose/` defaults are independent of this directory.

On the server, run `bash ../deploy/compose/setup.sh` once, then fill `.env`,
`envs/*.env` and `docker-compose.local.yml`. These local values are ignored by Git;
the Compose definitions, samples and start command are versioned.

From the backend repository, start devnet with:

```bash
./docker-compose.dev/start.sh --build
```

Use `./start.sh --build` from this directory, or `--no-build` to use selected images.
The command validates configuration before starting containers.

The existing devnet controller still uses its authenticated private snapshots.
Adopting this directory requires preserving that runtime binding; see the
[current devnet guide](../docs/deployment/dev-v10.md).
