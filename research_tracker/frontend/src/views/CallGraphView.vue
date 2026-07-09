<template>
  <div class="view-container">
    <div class="view-hint">
      调用图视图：展示函数/方法之间的 calls 关系。<b>单击</b>查看函数详情，<b>悬停</b>高亮调用链。
      默认显示全局调用关系（连接度最高的 400 个函数）；在星系视图选中一个 script 节点后，此处自动聚焦该脚本的调用链。
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
import { cytoscape, ELEMENT_STYLE, getCallgraphLayout } from '../cytoscapeStyle'
import type { GraphData, GraphNode, GraphIndex, GraphEdge } from '../types'
import { getNeighbors } from '../data/loader'
import ZoomControls from '../components/ZoomControls.vue'

const cyEl = ref<HTMLDivElement>()
let cy: cytoscape.Core | null = null

const props = defineProps<{
  graph: GraphData
  index: GraphIndex
  filters: { types: Set<string>; stages: Set<string>; status: Set<string>; showCollapsed: boolean }
  search: string
  focusScript: string | null   // 当前关注的脚本路径
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

function buildElements() {
  // Call Graph：只显示 function/method/class 节点 + calls 边
  // 若有 focusScript，只显示该脚本内的函数及其调用的函数
  const codeTypes = new Set(['ast_function', 'ast_method', 'ast_class', 'function', 'method', 'class'])

  let scopeIds: Set<string> | null = null
  if (props.focusScript) {
    // 收集该脚本下的所有 AST 节点
    scopeIds = new Set()
    for (const n of props.graph.nodes) {
      if (n.path === props.focusScript && codeTypes.has(n.type)) {
        scopeIds.add(n.id)
      }
    }
  }

  const nodesToAdd: any[] = []
  const visibleIds = new Set<string>()

  // 无 focusScript 时：先收集所有参与 calls 边的函数，限制数量避免卡顿
  let globalCallNodeIds: Set<string> | null = null
  if (!scopeIds) {
    globalCallNodeIds = new Set()
    for (const e of props.graph.edges) {
      if (e.type === 'calls') {
        globalCallNodeIds.add(e.source)
        globalCallNodeIds.add(e.target)
      }
    }
  }

  for (const node of props.graph.nodes) {
    if (!codeTypes.has(node.type)) continue
    if (scopeIds && !scopeIds.has(node.id)) {
      // 检查是否被 scope 内函数调用，是则也显示
      const inEdges = props.index.inEdges.get(node.id) || []
      const calledByScope = inEdges.some((e: GraphEdge) => e.type === 'calls' && scopeIds!.has(e.source))
      if (!calledByScope) continue
    } else if (globalCallNodeIds && !globalCallNodeIds.has(node.id)) {
      // 全局模式：只显示参与 calls 边的函数
      continue
    }
    if (!passFilters(node)) continue
    visibleIds.add(node.id)
    nodesToAdd.push({
      data: {
        id: node.id,
        label: node.label,
        type: node.type,
        importance: node.importance,
        status: node.status,
        child_count: 0,
        qual_name: node.qual_name,
      },
    })
  }

  // 计算每个节点的调用连接度（用于截断排序和布局分层）
  const degree = new Map<string, number>()
  for (const e of props.graph.edges) {
    if (e.type !== 'calls') continue
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }

  // 全局模式：节点过多时截断，优先保留连接度高的（调用/被调次数多）
  const MAX_GLOBAL_NODES = 200
  if (!scopeIds && nodesToAdd.length > MAX_GLOBAL_NODES) {
    nodesToAdd.sort((a, b) => (degree.get(b.data.id) || 0) - (degree.get(a.data.id) || 0))
    nodesToAdd.length = MAX_GLOBAL_NODES
    visibleIds.clear()
    for (const n of nodesToAdd) visibleIds.add(n.data.id)
  }

  const edgesToAdd: any[] = []
  for (const e of props.graph.edges) {
    if (e.type !== 'calls') continue
    if (!visibleIds.has(e.source) || !visibleIds.has(e.target)) continue
    edgesToAdd.push({
      data: {
        id: `${e.source}->${e.target}->calls`,
        source: e.source,
        target: e.target,
        type: 'calls',
        confidence: e.confidence,
      },
    })
  }

  return { nodes: nodesToAdd, edges: edgesToAdd, degree }
}

function passFilters(node: GraphNode): boolean {
  if (props.filters.status.size > 0 && !props.filters.status.has(node.status || 'active')) return false
  if (props.search) {
    const q = props.search.toLowerCase()
    const hay = `${node.label} ${node.qual_name || ''} ${node.docstring || ''}`.toLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

function renderGraph() {
  if (!cy) return
  const { nodes, edges, degree } = buildElements()
  cy.elements().remove()
  cy.add([...nodes, ...edges])
  if (cy.nodes().length === 0) return
  cy.layout(getCallgraphLayout(degree)).run()
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

  cy.on('tap', 'node', (evt) => {
    const node = props.index.nodeById.get(evt.target.id())
    emit('selectNode', node || null)
  })

  cy.on('mouseover', 'node', (evt) => {
    if (!cy) return
    const id = evt.target.id()
    const neighbors = getNeighbors(id, props.index)
    cy.elements().addClass('faded')
    evt.target.removeClass('faded').addClass('highlighted')
    neighbors.forEach(nid => cy!.$id(nid).removeClass('faded').addClass('highlighted'))
    cy.edges().forEach(edge => {
      if (edge.source().id() === id || edge.target().id() === id) {
        edge.removeClass('faded').addClass('highlighted')
      }
    })
  })
  cy.on('mouseout', 'node', () => {
    cy?.elements().removeClass('faded highlighted')
  })
  cy.on('tap', (evt) => {
    if (evt.target === cy) emit('selectNode', null)
  })
  cy.on('zoom', () => updateZoom())
  updateZoom()
})

watch(() => [props.filters, props.search, props.graph, props.focusScript], () => renderGraph(), { deep: true })

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
