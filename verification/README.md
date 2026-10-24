# Binary Release Verification

This directory contains release-validation tooling only, not FungiQ application sources.

The manually dispatched Windows workflow verifies the SHA256 of the uploaded release packages, initializes the previously published application, downloads a real full or delta update through Velopack, applies it with the native updater, and checks the restarted frozen application. All three workspaces and their default models are exercised by the application's built-in startup check. Every installed runtime file is checked against the release package.

Each edition/update-mode pair runs on its own temporary Windows VM. CUDA is disabled on these standard runners to verify CPU fallback; real CUDA inference is verified separately on the release workstation. The workflow does not publish releases or change release assets.

Reports, native update logs, and rendered workspace screenshots are retained as workflow artifacts for 14 days. The build manifest pins the expected package hashes.
