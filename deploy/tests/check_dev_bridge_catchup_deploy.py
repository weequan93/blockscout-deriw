#!/usr/bin/env python3
"""Simulate the dev catchup rollout with real Git/Compose and container fixtures."""
from check_dev_bridge_metrics_deploy import main

if __name__ == '__main__':
    main(catchup=True)
    main(catchup=True, fail_handoff=True)
