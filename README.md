# pytile — 本地图像金字塔与切片工具

`pytile` 在本地读取 PNG/JPEG 图片，生成逐级缩小的图像金字塔（image pyramid），
按固定 tile 尺寸切片，并输出记录全部校验信息的 `manifest.json`。
所有输入、缩放层、tile、manifest 与缓存都只存放在本地文件系统或内存中，
**不依赖地图服务器、CDN、云存储或任何外部服务**。

## 安装与依赖

- Python 3.10+
- Pillow（图像解码/缩放）
- pytest（仅测试需要）

```bash
pip install pillow pytest
```

## 使用方法

```bash
python -m pytile 输入图.png --output 输出目录 \
    --tile-size 256 --resample bilinear --min-size 256
```

参数说明：

- `--tile-size`：tile 边长（像素），默认 256。
- `--resample`：缩放方式，`nearest` 或 `bilinear`，默认 `bilinear`。
- `--min-size`：停止条件，当某一级的 `max(宽, 高) <= min-size` 时该级为最后一级，默认 256。

也可以在代码中调用：

```python
from pathlib import Path
from pytile import Config, build_pyramid

result = build_pyramid(Path("in.png"), Path("out"),
                       Config(tile_size=256, resample="bilinear", min_size=256))
```

## 层级算法

- 第 0 级为原图全分辨率。
- 第 `z+1` 级由第 `z` 级**逐级减半**（progressive halving）得到，
  尺寸为 `ceil(w/2) x ceil(h/2)`（奇数尺寸向上取整，不会丢像素）。
- 当某一级满足 `max(宽, 高) <= min-size` 时停止，该级为金字塔顶层。
- 缩放滤波支持 `nearest`（最近邻）与 `bilinear`（双线性）。
  相同输入图片 + 相同配置必然产生字节级一致的输出（tile 一律以
  固定参数的 PNG 写出，PNG 不嵌入时间戳），manifest 中的哈希可复现。

## 坐标与边缘规则

- tile 以 `(level, col, row)` 寻址，原点为图像**左上角**。
- tile `(col, row)` 覆盖像素矩形
  `[col*tile_size, (col+1)*tile_size) x [row*tile_size, (row+1)*tile_size)`。
- **边缘规则：裁剪（crop），不做 padding。** 右/下边缘不足一个完整
  tile 时，按实际剩余像素存储为较小的 tile；每个 tile 的真实宽高都
  记录在 manifest 中，消费方据此定位，无需猜测。
- 文件布局：`<输出目录>/tiles/<level>/<col>_<row>.png`。

## Manifest

`<输出目录>/manifest.json` 内容（字段完整、按 key 排序的 JSON）：

- `source`：原图文件名、像素尺寸、原图文件的 SHA-256。
- `config`：本次构建的 `tile_size` / `resample` / `min_size`。
- `edge_rule`：固定为 `"crop"`。
- `levels[]`：每级的 `level`、尺寸、`cols`/`rows`，以及 `tiles[]` —
  每个 tile 的 `col`、`row`、相对路径 `file`、实际 `width`/`height`
  和文件内容的 `sha256`。

## 增量重建

每次构建会先读取已有 manifest：

- 若原图 SHA-256 与配置均未变化，则逐个校验 manifest 中记录的 tile：
  文件存在且 SHA-256 一致的直接复用（不重写、不改 mtime）；
  **缺失或损坏（哈希不符）的 tile 只重新生成这些 tile**。
- 若原图或配置发生变化，则执行全量重建（输出目录由工具管理，
  旧 tile 会被清理，避免被误认为有效）。

## 原子写入与中断安全

- 每个 tile 先写入同目录下的临时文件（`.tmp-*`），`fsync` 后用
  `os.replace` 原子替换目标文件；构建开始时清理遗留的临时文件。
- `manifest.json` 在**所有 tile 成功之后**最后原子写入。
- 因此构建中途崩溃/中断绝不会留下"manifest 认为有效、实际是半文件"
  的状态：要么 manifest 是上一版完整状态，要么不存在。

## 运行测试

测试会自动生成小型测试图像，全部在终端断言校验，不打开任何图片窗口：

```bash
python -m pytest tests/ -v
```

覆盖场景：奇数尺寸与层级停止规则、边缘 tile 裁剪尺寸、nearest/bilinear
两种缩放的确定性与差异、增量重建（无变化时不重写任何 tile）、
tile 损坏/缺失后的定点重生成、输入或配置变化触发全量重建、
构建中途失败不污染 manifest 且不留临时文件、JPEG 输入。
