# DeriW production/mainnet

Use this directory for production's own `.env` and `envs/*.env` files. Setup,
startup and release commands are in [the environment-directory guide](../deploy/compose/README.md).
Populate the chain ID and settings from the existing production installation.
Promote the same compatible image digest qualified on testnet.
The original `docker-compose/` defaults are independent of this directory.

On the server, run `bash ../deploy/compose/setup.sh` once, then fill `.env`,
`envs/*.env` and `docker-compose.local.yml`. These local values are ignored by Git;
the Compose definitions, samples and start command are versioned.

From the backend repository, start production with:

```bash
./docker-compose.prod/start.sh --no-build
```

Use `./start.sh --no-build` from this directory to use the selected release images.
Use `--build` only when intentionally building this source on the server.
The command validates configuration before starting containers.
