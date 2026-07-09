// 节点与边的 TypeScript 类型定义

export type NodeType =
  | 'project' | 'subproject' | 'stage' | 'directory'
  | 'script' | 'function' | 'method' | 'class'
  | 'ast_function' | 'ast_class' | 'ast_method'
  | 'data_file' | 'report' | 'csv_table' | 'image'
  | 'config' | 'log' | 'notebook'

export type EdgeType =
  | 'contains' | 'imports' | 'calls' | 'reads' | 'writes'
  | 'documents' | 'validates' | 'derived_from' | 'depends_on'

export type Importance = 'critical' | 'high' | 'normal' | 'low' | 'archive'
export type Status = 'active' | 'deprecated' | 'experimental' | 'planned'

export interface GraphNode {
  id: string
  type: NodeType | string
  label: string
  path?: string
  qual_name?: string
  stage?: string
  subproject?: string
  subproject_id?: string
  stage_id?: string
  size?: number
  mtime?: number
  hash?: string
  lineno?: number
  end_lineno?: number
  docstring?: string
  has_main_guard?: boolean
  args?: string[]
  returns?: string
  parent?: string
  imports?: string[]
  base_classes?: string[]
  collapsed?: boolean
  importance?: Importance | string
  status?: Status | string
  note?: string
  tags?: string[]
  child_count?: number
  // 报告相关
  title?: string
  summary?: string
  warnings_count?: number
  blocking_count?: number
  headings_count?: number
  results?: { status: string; detail: string }[]
  // CSV 相关
  format?: string
  columns?: string[]
  row_count?: number
  key_fields?: string[]
  time_range?: string
  products?: string[]
  satellites?: string[]
}

export interface GraphEdge {
  source: string
  target: string
  type: EdgeType | string
  confidence: number
  evidence: string
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

// 图谱索引（由 loader.buildIndex 构建，供视图快速查询邻居/父子关系）
export interface GraphIndex {
  nodeById: Map<string, GraphNode>
  outEdges: Map<string, GraphEdge[]>
  inEdges: Map<string, GraphEdge[]>
  childrenByParent: Map<string, string[]>
  parentByChild: Map<string, string>
}

// 节点类型 → 颜色（星系视图用）
// 节点类型 → 颜色（降饱和的深蓝主题，让琥珀强调色成为视觉焦点）
export const NODE_COLORS: Record<string, string> = {
  project: '#F5A524',       // 琥珀：项目根，唯一暖色焦点
  subproject: '#5B8DEF',    // 钢蓝
  stage: '#7B6FD6',         // 雾紫
  directory: '#3A4A6B',     // 暗钢
  script: '#4A90D9',        // 蓝
  ast_function: '#4FB3A0',  // 青绿
  ast_class: '#D67898',     // 玫瑰
  ast_method: '#7FB37F',    // 草绿
  function: '#4FB3A0',
  class: '#D67898',
  method: '#7FB37F',
  data_file: '#9370DB',     // 紫
  report: '#E8C76A',        // 暗金
  csv_table: '#3FA7B0',     // 蓝绿
  image: '#B98ED6',         // 浅紫
  config: '#8A7B6B',        // 暖灰
  log: '#5A6680',           // 蓝灰
  notebook: '#C98ED6',      // 粉紫
  cluster: '#243556',       // 聚类簇节点（深蓝底）
}

// 节点类型 → 中文显示名
export const NODE_TYPE_LABELS: Record<string, string> = {
  project: '项目',
  subproject: '子项目',
  stage: '阶段',
  directory: '目录',
  script: '脚本',
  function: '函数',
  method: '方法',
  class: '类',
  ast_function: '函数',
  ast_class: '类',
  ast_method: '方法',
  data_file: '数据',
  report: '报告',
  csv_table: '数据表',
  image: '图片',
  config: '配置',
  log: '日志',
  notebook: 'Notebook',
}

// 边类型 → 颜色
export const EDGE_COLORS: Record<string, string> = {
  contains: '#888888',
  imports: '#4A90D9',
  calls: '#50C878',
  reads: '#9370DB',
  writes: '#FF6B6B',
  documents: '#F0E68C',
  validates: '#20B2AA',
  derived_from: '#FFB347',
  depends_on: '#FF4500',
}

// 节点类型 → 默认大小
export const NODE_SIZES: Record<string, number> = {
  project: 60,
  subproject: 45,
  stage: 35,
  script: 28,
  class: 24,
  function: 18,
  method: 16,
  ast_class: 24,
  ast_function: 18,
  ast_method: 16,
  data_file: 22,
  report: 24,
  csv_table: 20,
  config: 16,
  notebook: 22,
}

// 重要性 → 边框
export const IMPORTANCE_BORDER: Record<string, string> = {
  critical: '#FF0000',
  high: '#FFD700',
  normal: '#444444',
  low: '#222222',
  archive: '#333333',
}
