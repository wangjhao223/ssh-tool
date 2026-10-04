#!/usr/bin/env python3
"""Detached local WSL download entry point, not deployed remotely."""
import sys
import file_tools

if __name__ == '__main__':
    file_tools.worker(sys.argv[1], int(sys.argv[2]))
