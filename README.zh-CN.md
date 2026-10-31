# release-automation

集中管理 [ejfkdev](https://github.com/ejfkdev) 各 CLI 程序的 WinGet 清单和 npm 包装包。
每个程序只需要在 `programs/` 里加一个 TOML，不需要复制发布脚本，也不需要为每个程序单独建仓库。

本仓库不构建二进制，只读取各项目已有的 GitHub Release 资产，记录 SHA-256，生成：

- `winget/manifests/...`：WinGet 的 version / installer / locale 三个 YAML
- `npm/<name>/`：`package.json` + `install.js` + 每个命令一个 launcher
- `checksums.toml`：版本与各平台资产哈希

## 为什么这样组织

WinGet 的清单在 `microsoft/winget-pkgs`，npm 的包在 npm registry。发布渠道都不要求每个程序有独立 GitHub 仓库。
把 catalog 集中在一个仓库，新增程序只改一个 TOML；生成物提交进仓库，便于 review，也能用 `sync.py --check` 离线校验。

Homebrew tap 和 Scoop bucket 保持现状，继续作为分发端；它们不并入本仓库。

## 常用命令

```bash
# 按已提交的 checksums 重新生成（不联网）
python3 tool/sync.py

# 抓取所有程序的最新 release，更新哈希并重新生成
python3 tool/sync.py --refresh

# 校验生成物与 catalog 一致（CI 用）
python3 tool/sync.py --check

# 查看 release / npm / WinGet 的版本差异
python3 tool/status.py
python3 tool/status.py --plan --json

# 默认 dry-run，加 --yes 才真正发布
python3 tool/publish.py npm    --program ddc
python3 tool/publish.py winget --program ddc --yes

# 构建多命令 WinGet payload（不上传）
python3 tool/publish.py bundle --program pycdc --out dist
```

`--refresh` 对版本和哈希都已完整的程序会跳过，不重复下载资产。需要强制重算时加 `--force`。

## 新增程序

1. 确认 catalog 里列出的每个 target 在 release 里都有对应资产。
2. 复制一个 `programs/*.toml`，修改字段。
3. `python3 tool/sync.py --refresh --program <name>`
4. `python3 tool/sync.py --check`
5. 首次发布：`publish.py npm ... --yes` 和 `publish.py winget ... --yes`。

catalog 的完整字段、归档 `member`、多命令 `bundle` 的写法见英文 [README.md](README.md)。

## 需要的 Secrets

| Secret | 用途 |
| --- | --- |
| `NPM_TOKEN` | npm 发布；granular token，允许 publish 并绕过 2FA |
| `WINGET_TOKEN` | WinGet 提 PR；classic PAT，至少 `public_repo`。也用于上传多命令 payload 到各项目 release |
| `GITHUB_TOKEN` | Actions 自动提供；读取公开 release、提交生成物 |

WinGet 通过 `gh` 调 GitHub REST API 直接提交 PR（`tool/submit_winget.py`）：先同步 fork 的
`master`，从 upstream 建分支，把 3 个 YAML 写到 `manifests/...`，再用 upstream 模板开 PR。
不需要 Windows runner 或 wingetcreate。第一次提交新包需要签 Microsoft CLA；你的账号已在
PR #466817 通过 `license/cla`，后续无需重复确认。

## 已知限制

- `apix`、`vcc`、`dns`、`oss` 上游没有 Windows arm64 资产，因此 WinGet 只发 x64，npm 的 `cpu` 字段也只含 x64。
- `zaip`、`oss` 仓库没有 LICENSE 文件，WinGet 标记 `Proprietary`，npm 标记 `UNLICENSED`；上游补 LICENSE 后改 catalog 即可。
- `pycdc`、`zaip`、`saw` 是多命令程序，WinGet 用 zip payload（`InstallerType: zip` + `NestedInstallerFiles`）。
- npm 首次发布新包可能先出现 `0.0.0-stage` 占位版本，审核通过后才可安装。
- WinGet 包 ID `ejfkdev.<name>` 首次合并后不要再改，改了就是新包、重新审核。

网络较慢时，`sync.py` 可设 `GITHUB_DOWNLOAD_PROXY`，npm 安装器可设
`EJFKDEV_DOWNLOAD_PROXY`；两者都只改变下载路径，SHA-256 始终与 GitHub API 校验。