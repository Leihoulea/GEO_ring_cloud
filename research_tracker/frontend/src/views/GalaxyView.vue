<template>
  <div class="view-container">
    <div class="view-hint">
      星系视图：项目为中心 → 子项目/阶段为星系层 → 脚本/数据/报告为星球 → 函数/类为卫星。
      <b>单击</b>选中节点，<b>双击</b>展开/折叠聚合节点，<b>悬停</b>高亮邻居。
    </div>
    <div ref="cyEl" class="cy-container"></div>
    <ZoomControls
      :zoom-percent="zoomPercent"
      :pan-mode="panMode"
      @zoom-in="zoomIn"
      @zoom-out="zoomOut"
      @fit="fit"
      @reset="reset"
      @toggle-pan="togglePan"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { cytoscape, ELEMENT_STYLE, getGalaxyLayout } from '../cytoscapeStyle'
import type { GraphData, GraphNode, GraphIndex } from '../types'
import { getNeighbors } from '../data/loader'
import ZoomControls from '../components/ZoomControls.vue'

const cyEl = ref<HTMLDivElement>()
let cy: cytoscape.Core | null = null

const props = defineProps<{
  graph: GraphData
  index: GraphIndex
  filters: { types: Set<string>; stages: Set<string>; status: Set<string>; showCollapsed: boolean }
  search: string
  typeLabels: Record<string, string>
}>()

const emit = defineEmits<{
  (e: 'selectNode', node: GraphNode | null): void
}>()

const zoomPercent = ref(100)
const panMode = ref(false)
function zoomIn() { cy?.zoom({ level: cy.zoom() * 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); updateZoom() }
function zoomOut() { cy?.zoom({ level: cy.zoom() / 1.3, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } }); updateZoom() }
function fit() { cy?.fit(undefined, 60); updateZoom() }
function reset() { renderGraph(); setTimeout(() => { cy?.fit(undefined, 60); updateZoom() }, 100) }
function togglePan() {
  panMode.value = !panMode.value
  if (!cy) return
  cy.autoungrabify(!panMode.value)
  cy.userPanningEnabled(true)
}
function updateZoom() { if (cy) zoomPercent.value = Math.round(cy.zoom() * 100) }
// 记录已展开的聚合节点（默认折叠）
const expandedNodes = ref<Set<string>>(new Set())

// 聚类簇的聚合阈值：同类子节点超过此数则聚合成一个簇节点
const CLUSTER_THRESHOLD = 3
// 容器节点类型：始终遍历其子节点（不受类型筛选影响，否则子节点到不了画布）
const CONTAINER_TYPES = new Set(['project', 'subproject', 'stage', 'directory'])

// 构建可见元素：聚类折叠 + 筛选作为可见性遮罩（非遍历闸门）
function buildElements() {
  const nodesToAdd: any[] = []
  const edgesToAdd: any[] = []
  const visibleNodeIds = new Set<string>()   // 真实节点 id（用于连边）
  const clusterNodeIds = new Set<string>()   // 簇节点 id（虚拟）

  // 从 project 根开始 BFS
  const projectNode = props.graph.nodes.find(n => n.type === 'project')
  const roots: string[] = []
  if (projectNode) {
    roots.push(projectNode.id)
  } else {
    for (const n of props.graph.nodes) {
      if (!props.index.parentByChild.has(n.id)) roots.push(n.id)
    }
  }

  const stack = [...roots]
  const visited = new Set<string>()

  while (stack.length) {
    const id = stack.pop()!
    if (visited.has(id)) continue
    visited.add(id)
    const node = props.index.nodeById.get(id)
    if (!node) continue

    const kids = props.index.childrenByParent.get(id) || []
    const isExpanded = expandedNodes.value.has(id) || node.type === 'project'
    const hasKids = kids.length > 0

    // 容器节点始终加入画布（即使被类型筛掉，也要显示以便下钻）
    // 非容器节点才受类型/状态筛选影响
    const isContainer = CONTAINER_TYPES.has(node.type)
    if (!isContainer && !passFilters(node)) continue

    visibleNodeIds.add(id)
    nodesToAdd.push({
      data: {
        id: node.id,
        label: node.label,
        type: node.type,
        importance: node.importance,
        status: node.status,
        collapsed: !isExpanded && hasKids,
        child_count: hasKids ? kids.length : 0,
        path: node.path,
      },
    })

    if (!hasKids) continue

    if (isExpanded) {
      // 已展开：子节点全部入栈（继续 BFS，子节点各自决定可见性）
      for (const k of kids) stack.push(k)
    } else {
      // 未展开：把子节点按类型聚合成簇节点
      // 但若开了类型筛选，则符合条件的子节点单独漏出显示，其余聚合
      const typeFilterOn = props.filters.types.size > 0
      const groups = new Map<string, string[]>()  // type -> [childId]
      const leakIds: string[] = []                // 因筛选而单独显示的子节点
      for (const k of kids) {
        const kn = props.index.nodeById.get(k)
        if (!kn) continue
        if (typeFilterOn && props.filters.types.has(kn.type) && !CONTAINER_TYPES.has(kn.type)) {
          leakIds.push(k)
        } else if (!typeFilterOn) {
          const t = kn.type
          if (!groups.has(t)) groups.set(t, [])
          groups.get(t)!.push(k)
        }
        // typeFilterOn 但子节点类型不匹配：既不漏出也不聚合（隐藏）
      }

      // 生成簇节点
      for (const [t, childIds] of groups) {
        if (childIds.length >= CLUSTER_THRESHOLD) {
          const clusterId = `__cluster__${id}__${t}`
          clusterNodeIds.add(clusterId)
          nodesToAdd.push({
            data: {
              id: clusterId,
              type: 'cluster',
              cluster_label: props.typeLabels[t] || t,
              child_count: childIds.length,
              parent_node: id,
              cluster_type: t,
              cluster_children: childIds,
            },
          })
          edgesToAdd.push({
            data: { id: `${id}->${clusterId}->contains`, source: id, target: clusterId, type: 'contains', confidence: 1.0 },
          })
        } else {
          // 少于阈值的直接展开显示
          for (const k of childIds) {
            visibleNodeIds.add(k)
            const kn = props.index.nodeById.get(k)!
            nodesToAdd.push({
              data: {
                id: kn.id, label: kn.label, type: kn.type,
                importance: kn.importance, status: kn.status,
                child_count: (props.index.childrenByParent.get(k)?.length || 0),
                path: kn.path, collapsed: false,
              },
            })
            edgesToAdd.push({
              data: { id: `${id}->${k}->contains`, source: id, target: k, type: 'contains', confidence: 1.0 },
            })
          }
        }
      }

      // 漏出的子节点（因筛选）单独显示
      for (const k of leakIds) {
        stack.push(k)  // 入栈让其走正常可见性判断 + 连边
      }
    }
  }

  // 添加非 contains 边（calls/reads/writes/imports 等）：仅当两端都可见
  for (const e of props.graph.edges) {
    if (e.type === 'contains') continue
    if (visibleNodeIds.has(e.source) && visibleNodeIds.has(e.target)) {
      edgesToAdd.push({
        data: {
          id: `${e.source}->${e.target}->${e.type}`,
          source: e.source, target: e.target,
          type: e.type, confidence: e.confidence,
        },
      })
    }
  }

  return { nodes: nodesToAdd, edges: edgesToAdd }
}

function passFilters(node: GraphNode): boolean {
  // 类型过滤
  if (props.filters.types.size > 0 && !props.filters.types.has(node.type)) return false
  // 阶段过滤
  if (props.filters.stages.size > 0) {
    const sg = node.stage || ''
    if (sg && !props.filters.stages.has(sg)) return false
  }
  // 状态过滤
  if (props.filters.status.size > 0 && !props.filters.status.has(node.status || 'active')) return false
  // 搜索过滤（聚合节点不受搜索影响，保留以便下钻）
  if (props.search) {
    const isAgg = ['project', 'subproject', 'stage', 'directory'].includes(node.type)
    if (!isAgg) {
      const q = props.search.toLowerCase()
      const hay = `${node.label} ${node.path || ''} ${node.qual_name || ''} ${node.docstring || ''}`.toLowerCase()
      if (!hay.includes(q)) return false
    }
  }
  return true
}

function renderGraph() {
  if (!cy) return
  const { nodes, edges } = buildElements()
  cy.elements().remove()
  cy.add([...nodes, ...edges])
  cy.layout(getGalaxyLayout()).run()
}

onMounted(() => {
  if (!cyEl.value) return
  cy = cytoscape({
    container: cyEl.value,
    elements: [],
    style: ELEMENT_STYLE,
    wheelSensitivity: 0.2,
    minZoom: 0.05,
    maxZoom: 4,
  })

  renderGraph()

  // 单击：选中节点 → 发送详情
  cy.on('tap', 'node', (evt) => {
    const id = evt.target.id()
    // 簇节点：合成一个描述其内容的详情对象
    if (id.startsWith('__cluster__')) {
      const d = evt.target.data()
      emit('selectNode', {
        id,
        type: 'cluster',
        label: `${d.cluster_label} · ${d.child_count}`,
        child_count: d.child_count,
        note: `聚类节点：${d.child_count} 个 ${d.cluster_label}（双击展开父容器查看）`,
      } as GraphNode)
      return
    }
    const node = props.index.nodeById.get(id)
    emit('selectNode', node || null)
  })

  // 双击：展开/折叠聚合节点；双击簇节点则展开其父容器
  cy.on('dbltap', 'node', (evt) => {
    const id = evt.target.id()
    // 簇节点：展开其父容器
    if (id.startsWith('__cluster__')) {
      const parentNode = evt.target.data('parent_node')
      if (parentNode) {
        expandedNodes.value.add(parentNode)
        renderGraph()
      }
      return
    }
    const hasChildren = (props.index.childrenByParent.get(id)?.length || 0) > 0
    if (!hasChildren) return
    if (expandedNodes.value.has(id)) {
      expandedNodes.value.delete(id)
    } else {
      expandedNodes.value.add(id)
    }
    renderGraph()
  })

  // 悬停：高亮邻居
  cy.on('mouseover', 'node', (evt) => {
    if (!cy) return
    const id = evt.target.id()
    const neighbors = getNeighbors(id, props.index)
    cy.elements().addClass('faded')
    evt.target.removeClass('faded').addClass('highlighted')
    neighbors.forEach(nid => {
      cy!.$id(nid).removeClass('faded').addClass('highlighted')
    })
    // 高亮连接边
    cy.edges().forEach(edge => {
      const s = edge.source().id()
      const t = edge.target().id()
      if (s === id || t === id || (neighbors.has(s) && neighbors.has(t))) {
        edge.removeClass('faded').addClass('highlighted')
      }
    })
  })

  cy.on('mouseout', 'node', () => {
    if (!cy) return
    cy.elements().removeClass('faded highlighted')
  })

  cy.on('tap', (evt) => {
    if (evt.target === cy) emit('selectNode', null)
  })

  // 监听缩放变化更新百分比
  cy.on('zoom', () => updateZoom())
  updateZoom()
})

watch(() => [props.filters, props.search, props.graph], () => {
  renderGraph()
}, { deep: true })

onBeforeUnmount(() => {
  cy?.destroy()
  cy = null
})
</script>

<style scoped>
.view-container { width: 100%; height: 100%; position: relative; background: #0B1426; }
.view-hint {
  padding: 7px 14px; font-size: 11px; color: #7A8BAA;
  background: #0E1A30; border-bottom: 1px solid #243556;
}
.view-hint b { color: #F5A524; font-weight: 600; }
.cy-container { width: 100%; height: calc(100% - 32px); }
</style>
