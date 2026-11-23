#!/usr/bin/env python3
"""Detached local WSL download entry point, not deployed remotely."""
import sys
from remote_ws.download_stream import worker

if __name__ == '__main__':
    worker(sys.argv[1], int(sys.argv[2]))
