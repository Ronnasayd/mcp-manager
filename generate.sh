#!/bin/bash
burp &
uv run catalog-builder --config backends.json
