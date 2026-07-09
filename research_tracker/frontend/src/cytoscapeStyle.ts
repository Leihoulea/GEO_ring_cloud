// Cytoscape.js 共享样式与布局配置（深蓝星系主题）

import cytoscape from 'cytoscape'
import { NODE_COLORS, NODE_SIZES, EDGE_COLORS, IMPORTANCE_BORDER } from './types'

// 动态注册布局插件：若包未安装则静默回退到 cytoscape 自带布局
let hasCoseBilkent = false
let hasFcose = false
;(async () => {
  try {
    // @ts-ignore - 可选依赖
    const coseBilkent = await import('cytoscape-cose-bilkent')
    cytoscape.use((coseBilkent as any).default || coseBilkent)
    hasCoseBilkent = true
  } catch { /* 插件未安装，回退 cose */ }
  try {
    // @ts-ignore - 可选依赖
    const fcose = await import('cytoscape-fcose')
    cytoscape.use((fcose as any).default || fcose)
    hasFcose = true
  } catch { /* 插件未安装，回退 cose */ }
})()

export function galaxyLayoutName() { return hasCoseBilkent ? 'cose-bilkent' : 'cose' }
export function callgraphLayoutName() { return hasFcose ? 'fcose' : 'cose' }

// 主题色（与 types.ts 对齐，便于边框/标签统一引用）
const ACCENT = '#F5A524'
const TEXT = '#E6EDF7'
const TEXT_DIM = '#7A8BAA'

// cytoscape 的 Stylesheet 联合类型对 mapper 函数支持不友好，用 any 规避类型检查
export const ELEMENT_STYLE: any = [
  // ---- 普通节点 ----
  {
    selector: 'node',
    style: {
      // 标签策略：聚合节点（project/subproject/stage）和 script/report 始终显示；
      // 叶子节点（函数/类/数据）默认隐藏，悬停/选中时显示 —— 避免大量标签重叠
      'label': (ele: any) => {
        const t = ele.data('type')
        if (['project', 'subproject', 'stage', 'script', 'report'].includes(t)) return ele.data('label')
        return ''
      },
      'text-valign': 'bottom',
      'text-halign': 'center',
      'text-margin-y': 6,
      'text-wrap': 'wrap',
      'text-max-width': '110px',
      'font-size': (ele: any) => {
        const t = ele.data('type')
        if (t === 'project') return '15px'
        if (t === 'subproject') return '12px'
        if (t === 'stage') return '11px'
        return '10px'
      },
      'font-weight': 600,
      'color': TEXT,
      'text-outline-color': '#0B1426',
      'text-outline-width': 3,
      'background-color': (ele: any) => NODE_COLORS[ele.data('type')] || '#3A4A6B',
      'width': (ele: any) => {
        const t = ele.data('type')
        const base = NODE_SIZES[t] || 18
        const cc = ele.data('child_count') || 0
        if (cc > 0) return base + Math.min(24, Math.log2(cc + 1) * 5)
        return base
      },
      'height': (ele: any) => {
        const t = ele.data('type')
        const base = NODE_SIZES[t] || 18
        const cc = ele.data('child_count') || 0
        if (cc > 0) return base + Math.min(24, Math.log2(cc + 1) * 5)
        return base
      },
      'border-width': (ele: any) => {
        const imp = ele.data('importance')
        if (imp === 'critical') return 4
        if (imp === 'high') return 3
        return 1
      },
      'border-color': (ele: any) => {
        const imp = ele.data('importance')
        return IMPORTANCE_BORDER[imp] || '#243556'
      },
      'shape': (ele: any) => {
        const t = ele.data('type')
        if (t === 'project') return 'star'
        if (t === 'subproject') return 'diamond'
        if (t === 'stage') return 'hexagon'
        if (t === 'cluster') return 'round-diamond'
        if (t === 'class' || t === 'ast_class') return 'round-rectangle'
        if (t === 'function' || t === 'method' || t === 'ast_function' || t === 'ast_method') return 'ellipse'
        if (t === 'data_file' || t === 'csv_table') return 'rectangle'
        if (t === 'report') return 'round-triangle'
        return 'ellipse'
      },
      'opacity': 0.95,
    },
  },
  // ---- 聚类簇节点（同类子节点聚合） ----
  {
    selector: 'node[type = "cluster"]',
    style: {
      'label': (ele: any) => `${ele.data('cluster_label')} · ${ele.data('child_count')}`,
      'font-size': '10px',
      'background-color': '#1A2A4A',
      'border-color': '#3A5A8A',
      'border-width': 2,
      'border-style': 'dashed',
      'shape': 'round-diamond',
      'color': TEXT_DIM,
      'width': 44,
      'height': 44,
    },
  },
  // ---- 折叠节点（有子节点但未展开）：虚线边框 + + 号提示 ----
  {
    selector: 'node[?collapsed]',
    style: {
      'border-style': 'dashed',
      'border-width': 2,
    },
  },
  // ---- 悬停：显示标签 + 提亮 ----
  {
    selector: 'node:hover',
    style: {
      'label': 'data(label)',
      'font-size': '11px',
      'text-outline-width': 3,
      'z-index': 20,
      'overlay-color': ACCENT,
      'overlay-padding': '4px',
      'overlay-opacity': 0.15,
    },
  },
  // ---- 选中：琥珀边框（唯一强调色焦点） ----
  {
    selector: 'node:selected',
    style: {
      'label': 'data(label)',
      'border-width': 4,
      'border-color': ACCENT,
      'border-style': 'solid',
      'z-index': 15,
    },
  },
  // ---- 邻居高亮 ----
  {
    selector: '.highlighted',
    style: {
      'opacity': 1,
      'border-color': ACCENT,
      'border-width': 3,
      'z-index': 12,
    },
  },
  // ---- 淡化非邻居 ----
  {
    selector: '.faded',
    style: { 'opacity': 0.12 },
  },
  // ---- 边 ----
  {
    selector: 'edge',
    style: {
      'width': (ele: any) => 0.8 + ele.data('confidence') * 1.5,
      'line-color': (ele: any) => EDGE_COLORS[ele.data('type')] || '#3A4A6B',
      'target-arrow-color': (ele: any) => EDGE_COLORS[ele.data('type')] || '#3A4A6B',
      'target-arrow-shape': 'triangle',
      'curve-style': 'bezier',
      'arrow-scale': 0.7,
      'opacity': 0.45,
      'label': '',  // 边默认不显示标签，避免杂乱
    },
  },
  {
    selector: 'edge.highlighted',
    style: {
      'opacity': 1,
      'width': (ele: any) => 1.5 + ele.data('confidence') * 2.5,
      'line-color': ACCENT,
      'target-arrow-color': ACCENT,
      'label': 'data(type)',
      'font-size': '8px',
      'color': ACCENT,
      'text-background-color': '#0B1426',
      'text-background-opacity': 0.85,
      'text-background-padding': '1px',
      'text-rotation': 'autorotate',
    },
  },
  {
    selector: 'edge.faded',
    style: { 'opacity': 0.04 },
  },
]

// Galaxy 视图：concentric 同心圆，project 居中
export function getGalaxyLayout() {
  return {
    name: 'concentric',
    animate: true,
    animationDuration: 500,
    concentric: (ele: any) => {
      const t = ele.data('type')
      if (t === 'project') return 10
      if (t === 'subproject') return 8
      if (t === 'stage') return 6
      if (t === 'cluster') return 5
      if (t === 'script') return 4
      if (t === 'report') return 3
      return 2
    },
    levelWidth: () => 1,
    minNodeSpacing: 50,
    padding: 60,
    randomize: false,
  } as any
}

// Pipeline 视图：breadthfirst（cytoscape 自带）
export const PIPELINE_LAYOUT = {
  name: 'breadthfirst',
  directed: true,
  spacingFactor: 1.2,
  padding: 30,
  animate: true,
} as any

// Call Graph 视图：concentric 按连接度分层
export function getCallgraphLayout(degreeMap?: Map<string, number>) {
  return {
    name: 'concentric',
    animate: true,
    animationDuration: 500,
    concentric: (ele: any) => {
      const d = degreeMap ? (degreeMap.get(ele.id()) || 0) : 0
      return d + 1
    },
    levelWidth: () => 2,
    minNodeSpacing: 38,
    padding: 50,
    randomize: false,
  } as any
}

export { cytoscape }
