# fw360 — 360T7 固件自动解包（GitHub Actions）

把 360T7.bin 推到 GitHub，云端自动解包，然后你直接下载解好的文件。
**本地无需安装任何解包工具。**

## 产出物（一次下载全拿到）

| 文件 | 说明 |
|---|---|
| `vol_1.bin` | rootfs 卷重组流（含 squashfs） |
| `rootfs_from_vol1.squashfs` | 切好的 squashfs 镜像（23.8 MB，xz / v4.0） |
| `rootfs_from_vol1_rootfs.tar.gz` | **解包好的完整文件树**（www/、cgi-bin/、etc/ …） |
| `vol_0.bin` | 内核卷重组流 |
| `extract.log` | 解包日志（含超级块、metadata 校验结果） |

## 使用步骤

### 1. 在 GitHub 建一个空仓库
打开 https://github.com/new ，填名字（如 `fw360`），**不要**勾选任何初始化文件，创建。

> 公开仓库 Actions 完全免费；私有仓库每月 2000 分钟免费额度，本流程跑一次约 1 分钟。

### 2. 一键推送
本目录打开 PowerShell：

```powershell
.\push.ps1 -RepoUrl https://github.com/<你的用户名>/fw360.git
```

首次会弹出 GitHub 登录窗口（Git Credential Manager），登录后自动推送。
推送成功会打印 Actions 链接。

### 3. 下载解好的固件
- 打开 `https://github.com/<你的用户名>/fw360/actions`
- 等 `extract-360t7` 变绿（约 1 分钟）
- 点进那次运行 → 页面底部 **Artifacts** → 下载 `360t7-extracted.zip`
- 解压 zip，里面的 `rootfs_from_vol1_rootfs.tar.gz` 再解一层就是完整 rootfs 文件树

## 固件解包原理（备查）

360T7.bin 为 360 私有 `\x7fVRF` 容器 + UBI 镜像结构：

```
0x000000  360 VRF 头（\x7f'VRF'）
0x03A128  FDT (device tree)
0x0E89B0  UBI 镜像起点，PEB = 128 KiB
            EC  header @ +0x000  ('UBI#')
            VID header @ +0x800  ('UBI!')
            data       @ +0x1000
            · volume 0 = 内核   (27 LEBs)
            · volume 1 = rootfs (188 LEBs, 开头即 'hsqs')
            · layout volume (UBI 内部)
```

因 LEB 数据区（126976 B）< PEB（131072 B），每块之间夹 4 KiB UBI 头，
**不能直接 carve**，必须按 vol_id/lnum 重组 LEB 流后再切 squashfs。
`extract_fw.py` 已自动完成全部流程，并校验：

- squashfs 超级块（ver=4.0 / comp=4 xz / 3434 inodes）
- inode_table 首个 metadata 块 xz 解压（验证非魔改）

## 本地跑（可选）

```bash
python3 extract_fw.py 360T7.bin -o out        # 需要 PATH 里有 unsquashfs
python3 extract_fw.py 360T7.bin -o out --no-unsquashfs   # 只出镜像不解树
```
