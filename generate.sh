#!/bin/bash
burp &
python -m src.catalog.builder --config backends.json
