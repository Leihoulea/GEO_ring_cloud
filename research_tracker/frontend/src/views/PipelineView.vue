<template>
  <div class="view-container">
    <div class="view-hint">
      管道视图：按阶段分列，展示数据流向（data→script→output）。<b>单击</b>查看节点详情，<b>悬停</b>高亮上下游。
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
import { cytoscape, ELEMENT_STYLE } from '../cytoscapeStyle'
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
  // Pipeline 视图：只显示参与数据流边（reads/writes/derived_from/documents/imports）的节点
  // 避免把上千个孤立文件全画出来导致卡顿
  const visibleTypes = new Set(['script', 'data_file', 'report', 'csv_table', 'config', 'notebook', 'image'])
  const dataflowEdges = new Set(['reads', 'writes', 'derived_from', 'documents', 'imports'])

  // 先收集所有数据流边，确定参与边的节点
  const edgeNodes = new Set<string>()
  const filteredEdges: GraphEdge[] = []
  for (const e of props.graph.edges) {
    if (!dataflowEdges.has(e.type)) continue
    filteredEdges.push(e)
    edgeNodes.add(e.source)
    edgeNodes.add(e.target)
  }

  const nodesToAdd: any[] = []
  const visibleIds = new Set<string>()

  for (const node of props.graph.nodes) {
    if (!visibleTypes.has(node.type)) continue
    // 只显示参与数据流边的节点
    if (!edgeNodes.has(node.id)) continue
    if (!passFilters(node)) continue
    visibleIds.add(node.id)
    nodesToAdd.push({
      data: {
        id: node.id,
        label: node.label,
        type: node.type,
        importance: node.importance,
        status: node.status,
        stage: node.stage || '',
        child_count: 0,
      },
    })
  }

  const edgesToAdd: any[] = []
  for (const e of filteredEdges) {
    if (!visibleIds.has(e.source) || !visibleIds.has(e.target)) continue
    edgesToAdd.push({
      data: {
        id: `${e.source}->${e.target}->${e.type}`,
        source: e.source,
        target: e.target,
        type: e.type,
        confidence: e.confidence,
      },
    })
  }

  return { nodes: nodesToAdd, edges: edgesToAdd }
}

function passFilters(node: GraphNode): boolean {
  if (props.filters.types.size > 0 && !props.filters.types.has(node.type)) return false
  if (props.filters.stages.size > 0) {
    const sg = node.stage || ''
    if (sg && !props.filters.stages.has(sg)) return false
  }
  if (props.filters.status.size > 0 && !props.filters.status.has(node.status || 'active')) return false
  if (props.search) {
    const q = props.search.toLowerCase()
    const hay = `${node.label} ${node.path || ''}`.toLowerCase()
    if (!hay.includes(q)) return false
  }
  return true
}

function renderGraph() {
  if (!cy) return
  const { nodes, edges } = buildElements()
  cy.elements().remove()
  cy.add([...nodes, ...edges])
  // 按 stage 分列：把同 stage 节点设为同一 rank
  // breadthfirst 自动按层级，但为按 stage 排，用 grid 风格手动布局更清晰
  layoutByStage()
}

function layoutByStage() {
  if (!cy) return
  // 收集所有 stage（按出现顺序，更符合流水线次序）
  const stages: string[] = []
  cy.nodes().forEach(n => {
    const s = n.data('stage') || '(未阶段)'
    if (!stages.includes(s)) stages.push(s)
  })
  // 尝试按 stage 编号自然排序
  stages.sort((a, b) => {
    const na = parseInt(a.replace(/\D/g, '')) || 999
    const nb = parseInt(b.replace(/\D/g, '')) || 999
    if (na !== nb) return na - nb
    return a.localeCompare(b)
  })
  if (stages.length === 0) {
    cy.layout({ name: 'grid' } as any).run()
    return
  }
  const colWidth = 300
  const rowHeight = 55
  const maxPerCol = 60  // 每列最多显示节点，超出截断避免过长
  const startX = -((stages.length - 1) * colWidth) / 2
  stages.forEach((stage, colIdx) => {
    let colNodes = cy!.nodes().filter(n => (n.data('stage') || '(未阶段)') === stage)
    // 截断过长的列
    if (colNodes.length > maxPerCol) {
      colNodes = colNodes.slice(0, maxPerCol)
    }
    colNodes.forEach((n, rowIdx) => {
      n.position({
        x: startX + colIdx * colWidth,
        y: -((colNodes.length - 1) * rowHeight) / 2 + rowIdx * rowHeight,
      })
    })
  })
  cy.fit(undefined, 60)
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

watch(() => [props.filters, props.search, props.graph], () => renderGraph(), { deep: true })

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
