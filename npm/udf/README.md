# @ejfkdev/udf

Prebuilt [udf](https://github.com/ejfkdev/udf) binaries. The postinstall script
downloads the matching release asset and verifies its SHA-256; tar.gz and zip
assets are unpacked locally. If install scripts are disabled, a launcher
fetches the binary on first use instead.

```bash
npm install -g @ejfkdev/udf
udf --help
```

Commands:
- `udf`

Supported platforms: darwin-arm64, darwin-x64, linux-arm64, linux-x64, win32-arm64, win32-x64. Linux builds target glibc;
Alpine/musl users should install from source instead.

Each command accepts `<COMMAND>_BINARY_PATH` (for example
`UDF_BINARY_PATH`) to use a local
binary and skip the download.
