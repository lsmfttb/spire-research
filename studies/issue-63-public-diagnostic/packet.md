# 早期 MAP / REWARDS 公开信息诊断卡

> 自愿的分阶段人工诊断；仅呈现决策时 adapter public_input 的可见状态与选项。
> 原始证据来自公开仓库，因此这不是权限隔离或密码学盲审。
> 不要求唯一最佳答案；可以跳过。后续执行结果不在本包中。

## 抽样与阅读方式

- 固定总体：96 个自然多选根（MAP_SCREEN 35 个，REWARDS 61 个）。
- 分层规则：按 Issue 63 预注册的固定哈希规则，对两类分别取排序前 4；不替换、不按结果字段筛选。
- 案例编号匿名，按层内排序依次编号。本小样本仅作诊断，不估计策略错误发生率。
- 选项按输入顺序保留；每项的语义与完整公开候选动作身份紧邻呈现。
- 未记录或不可见值标明；不呈现历史轨迹、输入来源说明、样例来源标识、已抽取动作提示、模型判断或后续结果。

## M01 · 地图选择

### 当前可见信息
- 局面：MAP_SCREEN
- Act：1
- 层数：1
- 房间类型：MONSTER
- 当前 HP / 最大 HP：58 / 75
- 金币：117
- 已知 Boss：Hexaghost
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 11. Cleave；序号 11；类型 ATTACK；稀有度 COMMON；升级数 0；已升级 false
- 遗物：
  - name Slavers Collar；id_label SlaversCollar
- 药水：
  - potion_index 0；name Essence Of Steel；id_label EssenceOfSteel
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 1
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":5,"y":0}
- 该节点在公开地图图中的直接后继：[{"x":6,"y":1}]

### 各合法路线目的地的公开下游地图
- 按每个合法路线选项分别展开；从目的地起沿 public_state.map_graph 的有向边展示最多 4 跳。房型和坐标直接取公开图；跳数边界之外不呈现。
- 合法选项 #1，目的地 M@(6,1)：
  - 第 0 跳：M@(6,1) → M@(5,2), ?@(6,2)
  - 第 1 跳：M@(5,2) → M@(4,3), ?@(5,3)；?@(6,2) → M@(6,3)
  - 第 2 跳：M@(4,3) → ?@(3,4), M@(4,4)；?@(5,3) → M@(5,4)；M@(6,3) → M@(6,4)
  - 第 3 跳：?@(3,4) → E@(3,5)；M@(4,4) → E@(4,5)；M@(5,4) → E@(5,5)；M@(6,4) → E@(5,5)
  - 第 4 跳：E@(3,5)（预览边界；更下游未呈现）；E@(4,5)（预览边界；更下游未呈现）；E@(5,5)（预览边界；更下游未呈现）

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":6,"y":1}}`

   对应公开候选动作身份：

   `{"idx1":6,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=6 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"choice_type":"potion_discard","potion":{"id":19,"id_label":"EssenceOfSteel","name":"Essence Of Steel","potion_index":0}}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"game_potion_discard","label":"game.game_potion_discard idx1=0 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## M02 · 地图选择

### 当前可见信息
- 局面：MAP_SCREEN
- Act：1
- 层数：2
- 房间类型：MONSTER
- 当前 HP / 最大 HP：58 / 67
- 金币：136
- 已知 Boss：The Guardian
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Defend；序号 5；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Bash；序号 8；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 9. Wild Strike；序号 9；类型 ATTACK；稀有度 COMMON；升级数 0；已升级 false
  - 10. Sever Soul；序号 10；类型 ATTACK；稀有度 UNCOMMON；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":5,"y":1}
- 该节点在公开地图图中的直接后继：[{"x":5,"y":2},{"x":6,"y":2}]

### 各合法路线目的地的公开下游地图
- 按每个合法路线选项分别展开；从目的地起沿 public_state.map_graph 的有向边展示最多 4 跳。房型和坐标直接取公开图；跳数边界之外不呈现。
- 合法选项 #1，目的地 M@(5,2)：
  - 第 0 跳：M@(5,2) → M@(5,3)
  - 第 1 跳：M@(5,3) → M@(5,4), ?@(6,4)
  - 第 2 跳：M@(5,4) → R@(6,5)；?@(6,4) → R@(6,5)
  - 第 3 跳：R@(6,5) → E@(5,6), ?@(6,6)
  - 第 4 跳：E@(5,6)（预览边界；更下游未呈现）；?@(6,6)（预览边界；更下游未呈现）
- 合法选项 #2，目的地 $@(6,2)：
  - 第 0 跳：$@(6,2) → M@(5,3)
  - 第 1 跳：M@(5,3) → M@(5,4), ?@(6,4)
  - 第 2 跳：M@(5,4) → R@(6,5)；?@(6,4) → R@(6,5)
  - 第 3 跳：R@(6,5) → E@(5,6), ?@(6,6)
  - 第 4 跳：E@(5,6)（预览边界；更下游未呈现）；?@(6,6)（预览边界；更下游未呈现）

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":5,"y":2}}`

   对应公开候选动作身份：

   `{"idx1":5,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=5 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"$","x":6,"y":2}}`

   对应公开候选动作身份：

   `{"idx1":6,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=6 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## M03 · 地图选择

### 当前可见信息
- 局面：MAP_SCREEN
- Act：1
- 层数：0
- 房间类型：INVALID
- 当前 HP / 最大 HP：68 / 75
- 金币：99
- 已知 Boss：Slime Boss
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
- 遗物：
  - name Runic Cube；id_label Runic Cube
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"before_first_route"}
- 该节点在公开地图图中的直接后继：未记录/不可见（当前节点无唯一地图坐标；不表示候选目的地下游图不可见）

### 各合法路线目的地的公开下游地图
- 按每个合法路线选项分别展开；从目的地起沿 public_state.map_graph 的有向边展示最多 4 跳。房型和坐标直接取公开图；跳数边界之外不呈现。
- 合法选项 #1，目的地 M@(0,0)：
  - 第 0 跳：M@(0,0) → ?@(0,1)
  - 第 1 跳：?@(0,1) → M@(0,2)
  - 第 2 跳：M@(0,2) → ?@(1,3)
  - 第 3 跳：?@(1,3) → M@(0,4), ?@(2,4)
  - 第 4 跳：M@(0,4)（预览边界；更下游未呈现）；?@(2,4)（预览边界；更下游未呈现）
- 合法选项 #2，目的地 M@(1,0)：
  - 第 0 跳：M@(1,0) → M@(1,1), ?@(2,1)
  - 第 1 跳：M@(1,1) → M@(2,2)；?@(2,1) → M@(2,2)
  - 第 2 跳：M@(2,2) → ?@(1,3)
  - 第 3 跳：?@(1,3) → M@(0,4), ?@(2,4)
  - 第 4 跳：M@(0,4)（预览边界；更下游未呈现）；?@(2,4)（预览边界；更下游未呈现）
- 合法选项 #3，目的地 M@(5,0)：
  - 第 0 跳：M@(5,0) → M@(6,1)
  - 第 1 跳：M@(6,1) → ?@(5,2), M@(6,2)
  - 第 2 跳：?@(5,2) → ?@(4,3)；M@(6,2) → M@(5,3), ?@(6,3)
  - 第 3 跳：?@(4,3) → M@(4,4)；M@(5,3) → M@(6,4)；?@(6,3) → M@(6,4)
  - 第 4 跳：M@(4,4)（预览边界；更下游未呈现）；M@(6,4)（预览边界；更下游未呈现）

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":0,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=0 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":1,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":1,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=1 idx2=0 idx3=0","scope":"game"}`

3. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":5,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":5,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=5 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## M04 · 地图选择

### 当前可见信息
- 局面：MAP_SCREEN
- Act：1
- 层数：0
- 房间类型：INVALID
- 当前 HP / 最大 HP：68 / 75
- 金币：199
- 已知 Boss：Slime Boss
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"before_first_route"}
- 该节点在公开地图图中的直接后继：未记录/不可见（当前节点无唯一地图坐标；不表示候选目的地下游图不可见）

### 各合法路线目的地的公开下游地图
- 按每个合法路线选项分别展开；从目的地起沿 public_state.map_graph 的有向边展示最多 4 跳。房型和坐标直接取公开图；跳数边界之外不呈现。
- 合法选项 #1，目的地 M@(2,0)：
  - 第 0 跳：M@(2,0) → ?@(3,1)
  - 第 1 跳：?@(3,1) → ?@(3,2)
  - 第 2 跳：?@(3,2) → M@(2,3)
  - 第 3 跳：M@(2,3) → M@(1,4)
  - 第 4 跳：M@(1,4)（预览边界；更下游未呈现）
- 合法选项 #2，目的地 M@(4,0)：
  - 第 0 跳：M@(4,0) → M@(4,1)
  - 第 1 跳：M@(4,1) → M@(4,2)
  - 第 2 跳：M@(4,2) → M@(3,3), ?@(5,3)
  - 第 3 跳：M@(3,3) → M@(3,4)；?@(5,3) → M@(5,4)
  - 第 4 跳：M@(3,4)（预览边界；更下游未呈现）；M@(5,4)（预览边界；更下游未呈现）
- 合法选项 #3，目的地 M@(5,0)：
  - 第 0 跳：M@(5,0) → ?@(5,1), M@(6,1)
  - 第 1 跳：?@(5,1) → M@(4,2)；M@(6,1) → M@(5,2), ?@(6,2)
  - 第 2 跳：M@(4,2) → M@(3,3), ?@(5,3)；M@(5,2) → ?@(5,3)；?@(6,2) → M@(6,3)
  - 第 3 跳：M@(3,3) → M@(3,4)；?@(5,3) → M@(5,4)；M@(6,3) → M@(5,4)
  - 第 4 跳：M@(3,4)（预览边界；更下游未呈现）；M@(5,4)（预览边界；更下游未呈现）

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":2,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":2,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=2 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":4,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":4,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=4 idx2=0 idx3=0","scope":"game"}`

3. **公开选项**

   选择语义：

   `{"choice_type":"map_route","destination":{"location":"map_node","room_symbol":"M","x":5,"y":0}}`

   对应公开候选动作身份：

   `{"idx1":5,"idx2":0,"idx3":0,"kind":"map","label":"game.map idx1=5 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## R01 · 奖励选择

### 当前可见信息
- 局面：REWARDS
- Act：1
- 层数：2
- 房间类型：MONSTER
- 当前 HP / 最大 HP：73 / 75
- 金币：99
- 已知 Boss：Slime Boss
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 11. Bludgeon；序号 11；类型 ATTACK；稀有度 RARE；升级数 0；已升级 false
  - 12. Pommel Strike；序号 12；类型 ATTACK；稀有度 COMMON；升级数 0；已升级 false
  - 13. Pommel Strike；序号 13；类型 ATTACK；稀有度 COMMON；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":3,"y":1}
- 该节点在公开地图图中的直接后继：[{"x":2,"y":2}]

### 奖励选项类别与公开分组信息
- 当前 screen_context 及各合法选项语义均未记录奖励组 ID、组成员、共同领取或互斥字段：未记录/不可见。
- 按可见 choice_type 汇总（仅为语义类别，不代表实际领取分组或互斥规则）：
  - `gold`：合法选项 #1
  - `skip`：合法选项 #2
- 不据此推断不同类别或同类别选项能否共同领取；共同领取/互斥关系：未记录/不可见。

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"gold","gold":12}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_gold","label":"game.reward_gold idx1=0 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"choice_type":"skip","description":"Skip the remaining reward screen choices"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"skip","label":"game.skip idx1=0 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## R02 · 奖励选择

### 当前可见信息
- 局面：REWARDS
- Act：1
- 层数：1
- 房间类型：MONSTER
- 当前 HP / 最大 HP：60 / 82
- 金币：109
- 已知 Boss：The Guardian
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":5,"y":0}
- 该节点在公开地图图中的直接后继：[{"x":5,"y":1}]

### 奖励选项类别与公开分组信息
- 当前 screen_context 及各合法选项语义均未记录奖励组 ID、组成员、共同领取或互斥字段：未记录/不可见。
- 按可见 choice_type 汇总（仅为语义类别，不代表实际领取分组或互斥规则）：
  - `card`：合法选项 #1, #2, #3
  - `skip`：合法选项 #4
- 不据此推断不同类别或同类别选项能否共同领取；共同领取/互斥关系：未记录/不可见。

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"card":{"id":301,"id_label":"SHRUG_IT_OFF","name":"Shrug It Off","rarity":"COMMON","type":"SKILL","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"card":{"id":329,"id_label":"SWORD_BOOMERANG","name":"Sword Boomerang","rarity":"COMMON","type":"ATTACK","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":1,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=1 idx3=0","scope":"game"}`

3. **公开选项**

   选择语义：

   `{"card":{"id":198,"id_label":"IRON_WAVE","name":"Iron Wave","rarity":"COMMON","type":"ATTACK","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":2,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=2 idx3=0","scope":"game"}`

4. **公开选项**

   选择语义：

   `{"choice_type":"skip","description":"Skip the remaining reward screen choices"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"skip","label":"game.skip idx1=0 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## R03 · 奖励选择

### 当前可见信息
- 局面：REWARDS
- Act：1
- 层数：1
- 房间类型：MONSTER
- 当前 HP / 最大 HP：64 / 75
- 金币：99
- 已知 Boss：The Guardian
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 1；已升级 true
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":5,"y":0}
- 该节点在公开地图图中的直接后继：[{"x":5,"y":1},{"x":6,"y":1}]

### 奖励选项类别与公开分组信息
- 当前 screen_context 及各合法选项语义均未记录奖励组 ID、组成员、共同领取或互斥字段：未记录/不可见。
- 按可见 choice_type 汇总（仅为语义类别，不代表实际领取分组或互斥规则）：
  - `gold`：合法选项 #1
  - `card`：合法选项 #2, #3, #4
  - `skip`：合法选项 #5
- 不据此推断不同类别或同类别选项能否共同领取；共同领取/互斥关系：未记录/不可见。

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"choice_type":"gold","gold":16}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_gold","label":"game.reward_gold idx1=0 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"card":{"id":69,"id_label":"CLOTHESLINE","name":"Clothesline","rarity":"COMMON","type":"ATTACK","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=0 idx3=0","scope":"game"}`

3. **公开选项**

   选择语义：

   `{"card":{"id":249,"id_label":"POMMEL_STRIKE","name":"Pommel Strike","rarity":"COMMON","type":"ATTACK","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":1,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=1 idx3=0","scope":"game"}`

4. **公开选项**

   选择语义：

   `{"card":{"id":152,"id_label":"FIRE_BREATHING","name":"Fire Breathing","rarity":"UNCOMMON","type":"POWER","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":2,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=2 idx3=0","scope":"game"}`

5. **公开选项**

   选择语义：

   `{"choice_type":"skip","description":"Skip the remaining reward screen choices"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"skip","label":"game.skip idx1=0 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。

## R04 · 奖励选择

### 当前可见信息
- 局面：REWARDS
- Act：1
- 层数：2
- 房间类型：MONSTER
- 当前 HP / 最大 HP：60 / 75
- 金币：129
- 已知 Boss：Slime Boss
- 当前卡组（保留每张卡及其升级绑定）：
  - 0. Ascender's Bane；序号 0；类型 CURSE；稀有度 SPECIAL；升级数 0；已升级 false
  - 1. Strike；序号 1；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 2. Strike；序号 2；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 3. Strike；序号 3；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 4. Strike；序号 4；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 5. Strike；序号 5；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 6. Defend；序号 6；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 7. Defend；序号 7；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 8. Defend；序号 8；类型 SKILL；稀有度 BASIC；升级数 1；已升级 true
  - 9. Defend；序号 9；类型 SKILL；稀有度 BASIC；升级数 0；已升级 false
  - 10. Bash；序号 10；类型 ATTACK；稀有度 BASIC；升级数 0；已升级 false
  - 11. Whirlwind；序号 11；类型 ATTACK；稀有度 UNCOMMON；升级数 0；已升级 false
- 遗物：
  - name Burning Blood；id_label Burning Blood
- 药水：
  - potion_index 0；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
  - potion_index 1；name EMPTY_POTION_SLOT；id_label EMPTY_POTION_ID
- 药水容量 / 数量：2 / 0
- 钥匙：{"blue_key":false,"green_key":false,"red_key":false}
- 当前公开地图节点：{"location":"map_node","room_symbol":"M","x":3,"y":1}
- 该节点在公开地图图中的直接后继：[{"x":2,"y":2},{"x":4,"y":2}]

### 奖励选项类别与公开分组信息
- 当前 screen_context 及各合法选项语义均未记录奖励组 ID、组成员、共同领取或互斥字段：未记录/不可见。
- 按可见 choice_type 汇总（仅为语义类别，不代表实际领取分组或互斥规则）：
  - `card`：合法选项 #1, #2, #3
  - `potion`：合法选项 #4
  - `skip`：合法选项 #5
- 不据此推断不同类别或同类别选项能否共同领取；共同领取/互斥关系：未记录/不可见。

### 全部合法选项（保持 public_input 的原有顺序）
每项保留公开选择语义及其对应候选动作身份；序号仅为本卡展示顺序。

1. **公开选项**

   选择语义：

   `{"card":{"id":65,"id_label":"CLASH","name":"Clash","rarity":"COMMON","type":"ATTACK","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=0 idx3=0","scope":"game"}`

2. **公开选项**

   选择语义：

   `{"card":{"id":14,"id_label":"ARMAMENTS","name":"Armaments","rarity":"COMMON","type":"SKILL","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":1,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=1 idx3=0","scope":"game"}`

3. **公开选项**

   选择语义：

   `{"card":{"id":197,"id_label":"INTIMIDATE","name":"Intimidate","rarity":"UNCOMMON","type":"SKILL","upgraded":false},"choice_type":"card"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":2,"idx3":0,"kind":"reward_card","label":"game.reward_card idx1=0 idx2=2 idx3=0","scope":"game"}`

4. **公开选项**

   选择语义：

   `{"choice_type":"potion","potion":{"id":41,"id_label":"Strength Potion","name":"Strength Potion"}}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"reward_potion","label":"game.reward_potion idx1=0 idx2=0 idx3=0","scope":"game"}`

5. **公开选项**

   选择语义：

   `{"choice_type":"skip","description":"Skip the remaining reward screen choices"}`

   对应公开候选动作身份：

   `{"idx1":0,"idx2":0,"idx3":0,"kind":"skip","label":"game.skip idx1=0 idx2=0 idx3=0","scope":"game"}`

### 请仅依据当前可见信息自愿判断
1. 是否有明显风险较高/可能较差的选项，或值得进一步比较的短名单？不必指定唯一最佳项。
2. 哪些可见事实或决策机制对判断重要？
3. 这个案例是否有歧义？可以跳过。
