# pal4_csb_csv_tool.py 读写 CSB 文件算法说明

## 概览

这个脚本的核心思路不是“解析完整的 CSB 格式”，而是基于一个更务实的假设：

- CSB 文件内部存在大量“小端 `u32` 长度 + 紧随其后的字符串字节”的片段。
- 这些字符串大多使用 `gbk` 编码。
- 真正需要翻译的文本，通常包含中文字符，并且在统计特征上可以和路径名、标识符、噪声数据区分开。

因此，脚本采用的是一种“扫描长度前缀字符串块 -> 过滤出可见文本 -> 导出 CSV -> 按偏移安全回写”的算法。

它并不构建整套 CSB 抽象语法树，而是做局部、可验证、可回滚的二进制文本替换。

## 一、底层读写原语

脚本先定义了两个最基础的二进制操作：

- `read_u32(data, offset)`：从 `offset` 位置读取一个小端无符号 32 位整数。
- `write_u32(data, offset, value)`：向 `offset` 位置写入一个小端无符号 32 位整数。

这两个函数是整个算法的基础，因为脚本把每一段候选文本都视为：

```text
[4 字节长度][payload 字节串]
```

若长度字段值为 `n`，则紧接着的 `n` 个字节被当作一个候选字符串负载。

## 二、输入文件收集算法

`collect_csb_files(inputs)` 的处理流程如下：

1. 遍历命令行传入的每个路径。
2. 如果路径是单个 `.csb` 文件，则直接加入结果。
3. 如果路径是目录，则递归搜索该目录下全部 `.csb` 文件。
4. 对收集结果做一次按绝对路径去重，避免同一个文件因不同相对路径被重复处理。

这一层不涉及 CSB 格式本身，作用是保证导出和导入面对的是稳定的一组目标文件。

## 三、可见文本判定算法

脚本不会把所有长度前缀字节串都导出，而是先经过 `is_visible_text(text, min_chars, strict)` 过滤。

### 1. 基础判定

任意候选字符串要通过下面几关：

1. 去掉首尾空白后，长度不能小于 `min_chars`。
2. 不能包含 `\x00`。
3. 必须至少包含一个中日韩统一表意文字字符。

如果导出模式是 `all`，到这里就通过，不再做更严格判定。

### 2. 严格模式判定

默认使用 `strict` 模式，还会继续排除一些“看起来不像台词”的字符串：

- 如果整个字符串像标识符或路径，只由 `A-Za-z0-9_./:-` 组成，则排除。
- 统计字符串中的 CJK 字符数量。
- 如果 CJK 数量少于 2，且又不包含明显的中文标点或富文本标记，例如 `：，。！？`、`<colour>`、`<dc0>` 等，则排除。
- 如果字符串首字符是 ASCII 英文字母，并且 CJK 数量少于 2，也排除。

### 3. 设计意图

这套规则的目的是减少误提取，例如：

- 路径名
- 资源键名
- 调试标识符
- 混杂少量中文的非显示数据

它本质上是启发式过滤，不保证语义 100% 正确，但能大幅提高导出的可翻译文本密度。

## 四、扫描 CSB 的导出算法

真正的扫描逻辑在 `iter_length_prefixed_strings(data, encoding, min_chars, max_bytes, strict)`。

### 1. 顺序扫描

算法对整个文件做线性遍历：

1. 对每个字节偏移 `offset`，读取 `read_u32(data, offset)` 作为候选长度 `payload_len`。
2. 如果 `payload_len <= 0` 或 `payload_len > max_bytes`，直接跳过。
3. 计算：

   - 字符串起点 `start = offset + 4`
   - 字符串终点 `end = start + payload_len`

4. 若 `end` 超过文件边界，跳过。

### 2. 解码与文本过滤

对 `data[start:end]`：

1. 按指定编码（默认 `gbk`）解码。
2. 解码失败则跳过。
3. 调用 `is_visible_text(...)` 判断是否为可见文本。
4. 不满足条件则跳过。

### 3. 去重策略

脚本用 `(start, end)` 作为唯一键放入 `seen_ranges`。

原因是线性扫描时，不同偏移位置有可能“误读”出相同的字符串负载区域。如果不去重，CSV 里会出现重复行。

### 4. 导出记录结构

每个通过过滤的候选项会产出一条记录：

- `length_offset`：长度字段的偏移
- `text_offset`：文本字节串的起始偏移
- `byte_length`：原始字节长度
- `original_text`：解码后的原文

外层 `export_csv()` 再补上：

- `file`
- `translation`（初始为空）

最终统一写入 UTF-8 BOM 的 CSV，便于表格软件打开。

## 五、导入前的数据分组算法

`import_csv(args)` 读入 CSV 后，先做一轮预处理：

1. 读取所有行。
2. 如果 CSV 为空，直接报错退出。
3. 跳过以下记录：

   - `translation` 为空
   - `translation` 与 `original_text` 完全相同

4. 按 `file` 字段把待写入记录分组，得到：

```text
{ 文件路径: [该文件的所有待替换行] }
```

这样做的目的是把每个文件作为独立事务处理，避免多个文件交叉修改时偏移互相干扰。

## 六、导入写回算法

这是脚本最关键的部分。它不是简单地“按 CSV 偏移写新字符串”，而是带有基线校验和偏移修正。

### 1. 每个文件单独加载

对每个待修改的 CSB 文件：

1. 读入整个文件到 `bytearray`，便于原地切片替换。
2. 初始化：

   - `changed = False`
   - `cumulative_delta = 0`

其中 `cumulative_delta` 表示此前所有长度变化累计造成的偏移漂移。

### 2. 按原始文本偏移排序

同一文件内的 CSV 行，按 `text_offset` 从小到大排序。

这一步很关键，因为一旦前面的字符串长度发生变化，后面的所有偏移都要整体平移。如果逆序或乱序处理，`cumulative_delta` 就无法正确工作。

### 3. 计算当前有效偏移

对每条记录：

- `base_length_offset` / `base_text_offset` 来自 CSV 基线。
- 实际写入位置则改为：

```text
length_offset = base_length_offset + cumulative_delta
text_offset   = base_text_offset + cumulative_delta
```

含义是：CSV 存的是“导出当时的偏移”，但文件可能已因前面若干替换变长或变短，所以当前真正位置要加上累计偏移差。

### 4. 双重基线校验

在真正替换前，脚本会做两层验证。

#### 校验 A：长度字段一致

读取当前文件中 `length_offset` 处的长度 `current_len`，要求它必须等于 CSV 中记录的 `old_len`。

如果不一致，说明当前文件已经不是导出 CSV 时的那个版本，或者之前的替换逻辑破坏了结构，此时立即终止，并提示重新从当前基线导出。

#### 校验 B：原文字节一致

读取当前文件中 `text_offset : text_offset + old_len` 的字节，必须与：

```text
original_text.encode(args.encoding)
```

完全一致。

这一层校验用来确认：

- 偏移没有算错
- 文件内容没有被外部改动
- CSV 和目标文件确实来自同一个基线

只有两层都通过，脚本才允许写入。

### 5. 两种替换模式

将 `translation` 用指定编码编码为 `new_raw` 后，有两种策略。

#### 模式 A：`exact`

要求 `len(new_raw) == old_len`。

若新旧字节长度不同，直接报错退出。这是最保守、风险最低的模式，因为它不改变后续数据布局。

#### 模式 B：`expand`

允许新旧字节长度不同。

替换后更新：

```text
cumulative_delta += new_len - old_len
```

也就是说，只要某条文本变长或变短，后续所有记录的有效偏移都会自动平移。

### 6. 实际写入方式

无论 `new_len` 是否等于 `old_len`，脚本都使用：

```text
data[text_offset : text_offset + old_len] = new_raw
```

在 Python 的 `bytearray` 里，这种切片赋值允许替换区间长度变化，因此：

- 等长时是原地覆盖
- 变长时会自动扩容
- 变短时会自动收缩

随后再把 `length_offset` 处的长度字段写成 `new_len`。

因此，文本负载和它前面的长度字段会保持一致。

## 七、文件头大小字段修正

每个文件完成全部替换后，如果确实发生了修改，脚本还会执行：

```text
write_u32(data, 0, len(data) - 4)
```

这表示作者在 PAL4 的 CSB 样本中观察到：

- 文件头偏移 `0` 处的 `u32` 似乎记录的是 `文件总长度 - 4`

所以在发生扩容或收缩后，需要同步修正这个头部值，否则文件总长信息会过期。

这一点不是通用二进制规则，而是基于样本经验得出的格式约定。

## 八、备份与提交写盘

如果启用了 `--backup`，脚本会先创建一个带时间戳的备份文件：

```text
原文件名.bak.YYYYMMDD_HHMMSS
```

然后再把修改后的 `bytearray` 整体写回原文件。

这使得导入过程具备最基本的可回退能力。

## 九、算法的安全性特点

这个脚本的安全性主要来自四个方面：

1. 基于原始偏移写回，而不是模糊搜索替换。
2. 写回前核对长度字段。
3. 写回前核对原文字节。
4. 允许可选备份。

所以它不是“盲写”，而是“基线一致时才写”。

## 十、算法边界与局限

这套方法有效，但必须明确它的边界。

### 1. 它不是完整格式解析器

脚本只识别“像长度前缀字符串的数据块”，并不知道这些字符串在 CSB 内部属于哪个逻辑结构。

因此它适合做文本提取与回填，不适合做深层结构编辑。

### 2. `expand` 依赖隐含前提

`expand` 模式默认认为：

- 字符串后续数据主要按顺序排列
- 结构内部不存在需要手工同步修正的大量绝对偏移表

如果 CSB 某些区域含有复杂的偏移索引、跳表或交叉引用，那么仅更新字符串长度和文件头大小可能还不够。

### 3. 导出是启发式提取

由于使用了中文特征过滤：

- 可能漏掉一些有效文本
- 也可能保留少量非台词数据

也就是说，导出阶段追求的是“高命中率 + 可人工校对”，而不是绝对完备。

## 十一、可以把算法概括成一条主线

如果把整个脚本压缩成一句话，它的工作流就是：

```text
遍历二进制 -> 把每个位置尝试解释成“长度前缀字符串” -> 用启发式规则筛出可见中文 -> 记录精确偏移 -> 回写时验证基线一致 -> 更新字符串长度与文件总长
```

## 十二、伪代码总结

```text
export:
  files = 收集全部 csb 文件
  for file in files:
    data = 读取整个文件
    for offset in 0..len(data)-4:
      n = read_u32(data, offset)
      if n 非法: continue
      raw = data[offset+4 : offset+4+n]
      if raw 不能按 gbk 解码: continue
      text = decode(raw)
      if text 不是可见中文文本: continue
      输出一行 CSV(file, length_offset, text_offset, byte_length, original_text)

import:
  rows = 读取 CSV
  按 file 分组，过滤掉空翻译和未修改行
  for file, file_rows in grouped:
    data = bytearray(读取文件)
    cumulative_delta = 0
    for row in 按 text_offset 升序排序(file_rows):
      length_offset = row.length_offset + cumulative_delta
      text_offset = row.text_offset + cumulative_delta
      校验当前长度 == row.byte_length
      校验当前字节 == row.original_text.encode(encoding)
      new_raw = row.translation.encode(encoding)
      若 exact 且长度变化: 报错
      用 new_raw 替换 data[text_offset : text_offset + old_len]
      write_u32(data, length_offset, len(new_raw))
      cumulative_delta += len(new_raw) - old_len
    write_u32(data, 0, len(data) - 4)
    写回文件
```

## 十三、结论

这个脚本的算法可以理解为一种“带基线校验的二进制文本补丁器”。

它的优点是：

- 实现简单
- 对 PAL4 这类脚本资源足够实用
- 导出和回写都保留了精确偏移信息
- 在不完整掌握格式规范的前提下，也能相对安全地进行文本替换

它的前提则是：

- 目标文本确实以长度前缀字符串形式存在
- 编码已知
- 变长替换不会破坏更深层的内部引用结构

在这个前提成立时，这个算法是一个很高性价比的 CSB 文本处理方案。