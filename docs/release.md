# Android Release Setup

The source repository is private. Android update files are published to the
separate public repository `Cloudsflee/Relayterm-releases`, so a phone does not
need a GitHub credential to download an APK.

## One-time setup

1. Create a **public repository** named `Relayterm-releases` under the
   `Cloudsflee` account and initialize it with a README. Do not add source code
   or secrets to it.
2. Create a release keystore on a trusted machine and keep a backup:

   ```powershell
   keytool -genkeypair -v -storetype JKS -keystore relayterm-release.jks `
     -alias relayterm -keyalg RSA -keysize 4096 -validity 10000
   ```

3. In the private `Relayterm` repository, add these Actions secrets:

   - `ANDROID_KEYSTORE_B64`: Base64 content of the keystore
   - `ANDROID_KEYSTORE_PASSWORD`
   - `ANDROID_KEY_ALIAS`
   - `ANDROID_KEY_PASSWORD`
   - `RELEASES_TOKEN`: a fine-grained PAT with `Contents: Read and write`
     only on `Cloudsflee/Relayterm-releases`

   Convert the keystore to Base64 in PowerShell without committing it:

   ```powershell
   [Convert]::ToBase64String(
     [IO.File]::ReadAllBytes('.\relayterm-release.jks')
   ) | Set-Clipboard
   ```

The keystore is intentionally ignored by `.gitignore`. Losing it means future
APK files cannot update an existing installation, so keep an offline backup.

## Publish a version

The workflow runs for semantic-version tags:

```powershell
git tag v0.1.1
git push origin v0.1.1
```

The workflow builds a signed APK, verifies its signature, creates
`RelayTerm.apk` and `RelayTerm.apk.sha256`, and publishes both to the public
repository's Release page. The stable download URL is:

```text
https://github.com/Cloudsflee/Relayterm-releases/releases/latest/download/RelayTerm.apk
```

Install Obtainium on the phone and add:

```text
https://github.com/Cloudsflee/Relayterm-releases
```

Android still asks for installation confirmation for a sideloaded update. The
first release signed with the new release keystore is a one-time migration from
the current debug APK; uninstall the debug build before installing that first
release. Later releases update in place as long as the keystore is preserved.
