# Hikvision CCTV Monitor

Python monitoring application for 12 Hikvision NVRs and approximately
360 expected camera channels.

## Current stage

The application currently validates the NVR inventory. ISAPI monitoring,
recording checks, storage health checks, and reporting are not implemented yet.

## Requirements

- Python 3.12
- Git
- Windows development environment

## Local setup

Create the virtual environment:

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1

Validate the inventory:

    python -m app.main

Run unit tests:

    python -m unittest discover -s tests -v

## Configuration and security

- config/nvrs.example.json contains documentation IP addresses.
- config/nvrs.local.json contains the real development inventory.
- Do not commit real inventory files, passwords, tokens, private keys,
  production reports, or sensitive logs.
- Production configuration will be stored outside the source repository.

## Planned capabilities

- Network connectivity and authentication checks
- NVR device identity and firmware discovery
- Camera/channel status
- Recording evidence and HDD health
- Structured JSON reports
- Google Sheets integration and daily email reporting
- Docker deployment, health checks, and rollback
- Daily reporting at 08:00 Asia/Phnom_Penh
