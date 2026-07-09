<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark">◎</span>
        <span class="brand-name">Research Tracker</span>
        <span class="brand-sub">科研项目知识图谱</span>
      </div>
      <div class="topbar-stats">
        <span>{{ stats.nodes.toLocaleString() }} 节点</span>
        <span class="sep">·</span>
        <span>{{ stats.edges.toLocaleString() }} 关系</span>
      </div>
    </header>

    <div class="body">
      <FilterPanel
        :view="view"
        :search="search"
        :filters="filters"
        :all-types="allTypes"
        :all-stages="allStages"
        :type-counts="typeCounts"
        :stage-counts="stageCounts"
        :stats="stats"
        @change-view="view = $event"
        @update:search="search = $event"
        @toggle-type="toggleType"
        @toggle-stage="toggleStage"
        @toggle-status="toggleStatus"
        @clear-types="filters.types.clear()"
      />

      <main class="view-area">
        <GalaxyView
          v-if="view === 'galaxy'"
          :graph="graph"
          :index="index"
          :filters="filters"
          :search="search"
          :type-labels="NODE_TYPE_LABELS"
          @select-node="onSelectNode"
        />
        <PipelineView
          v-else-if="view === 'pipeline'"
          :graph="graph"
          :index="index"
          :filters="filters"
          :search="search"
          @select-node="onSelectNode"
        />
        <CallGraphView
          v-else
          :graph="graph"
          :index="index"
          :filters="filters"
          :search="search"
          :focus-script="focusScript"
          @select-node="onSelectNode"
        />
      </main>

      <!-- 详情抽屉：选中节点时滑出 -->
      <transition name="drawer">
        <aside v-if="selectedNode" class="detail-drawer">
          <button class="drawer-close" @click="selectedNode = null" title="关闭">✕</button>
          <NodeDetail :node="selectedNode" />
        </aside>
      </transition>
    </div>

    <div v-if="loading" class="loading">加载图谱中…</div>
    <div v-else-if="graph.nodes.length === 0" class="loading">
      未找到 graph.json。请先运行后端：<br />
      <code>python backend/run_scan.py 你的项目根目录</code>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import FilterPanel from './components/FilterPanel.vue'
import NodeDetail from './components/NodeDetail.vue'
import GalaxyView from './views/GalaxyView.vue'
import PipelineView from './views/PipelineView.vue'
import CallGraphView from './views/CallGraphView.vue'
import { loadGraph, buildIndex } from './data/loader'
import type { GraphData, GraphNode, GraphIndex } from './types'
import { NODE_TYPE_LABELS } from './types'

const graph = ref<GraphData>({ nodes: [], edges: [] })
const index = ref<GraphIndex>(buildIndex({ nodes: [], edges: [] }))
const loading = ref(true)
const view = ref<string>('galaxy')
const search = ref('')
const selectedNode = ref<GraphNode | null>(null)
const focusScript = ref<string | null>(null)

const filters = reactive({
  types: new Set<string>(),
  stages: new Set<string>(),
  status: new Set<string>(),
  showCollapsed: true,
})

const allTypes = computed(() => {
  const set = new Set<string>()
  for (const n of graph.value.nodes) set.add(n.type)
  return Array.from(set).sort()
})

const allStages = computed(() => {
  const set = new Set<string>()
  for (const n of graph.value.nodes) {
    if (n.stage) set.add(n.stage)
  }
  return Array.from(set).sort()
})

const typeCounts = computed(() => {
  const c: Record<string, number> = {}
  for (const n of graph.value.nodes) c[n.type] = (c[n.type] || 0) + 1
  return c
})

const stageCounts = computed(() => {
  const c: Record<string, number> = {}
  for (const n of graph.value.nodes) {
    if (n.stage) c[n.stage] = (c[n.stage] || 0) + 1
  }
  return c
})

const stats = computed(() => ({
  nodes: graph.value.nodes.length,
  edges: graph.value.edges.length,
}))

function toggleType(t: string) {
  if (filters.types.has(t)) filters.types.delete(t)
  else filters.types.add(t)
}
function toggleStage(s: string) {
  if (filters.stages.has(s)) filters.stages.delete(s)
  else filters.stages.add(s)
}
function toggleStatus(s: string) {
  if (filters.status.has(s)) filters.status.delete(s)
  else filters.status.add(s)
}

function onSelectNode(node: GraphNode | null) {
  selectedNode.value = node
  // 若选中 script，设为 call graph 焦点
  if (node && (node.type === 'script' || node.type === 'data_file')) {
    focusScript.value = node.path || node.id
  }
}

onMounted(async () => {
  const g = await loadGraph()
  graph.value = g
  index.value = buildIndex(g)
  loading.value = false
})
</script>

<style scoped>
.app {
  width: 100%; height: 100%;
  display: flex; flex-direction: column;
  background: #0B1426;
  font-family: 'Space Grotesk', -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
/* 顶部栏 */
.topbar {
  height: 48px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 18px;
  background: #0E1A30;
  border-bottom: 1px solid #243556;
}
.brand { display: flex; align-items: baseline; gap: 10px; }
.brand-mark { color: #F5A524; font-size: 18px; line-height: 1; }
.brand-name { font-size: 15px; font-weight: 700; color: #E6EDF7; letter-spacing: 0.02em; }
.brand-sub { font-size: 11px; color: #7A8BAA; }
.topbar-stats { font-size: 12px; color: #7A8BAA; font-family: 'JetBrains Mono', monospace; }
.topbar-stats .sep { margin: 0 6px; color: #3A4A6B; }

/* 主体：左侧栏 + 图谱 + 抽屉 */
.body { flex: 1; display: flex; min-height: 0; position: relative; }
.view-area { flex: 1; min-width: 0; }

/* 详情抽屉 */
.detail-drawer {
  position: absolute; top: 0; right: 0; bottom: 0;
  width: 340px;
  background: #13203A;
  border-left: 1px solid #243556;
  box-shadow: -8px 0 24px rgba(0,0,0,0.35);
  overflow: hidden;
  z-index: 50;
}
.drawer-close {
  position: absolute; top: 10px; right: 12px; z-index: 2;
  width: 26px; height: 26px;
  background: #1A2A4A; border: 1px solid #243556; color: #7A8BAA;
  border-radius: 5px; cursor: pointer; font-size: 13px; line-height: 1;
}
.drawer-close:hover { background: #243556; color: #E6EDF7; }

/* 抽屉滑入动画 */
.drawer-enter-active, .drawer-leave-active { transition: transform 0.28s cubic-bezier(0.4, 0, 0.2, 1); }
.drawer-enter-from, .drawer-leave-to { transform: translateX(100%); }

/* 加载/空态 */
.loading {
  position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
  color: #7A8BAA; font-size: 14px; text-align: center;
}
.loading code {
  display: block; margin-top: 12px; color: #F5A524;
  background: #13203A; border: 1px solid #243556;
  padding: 10px 14px; border-radius: 6px;
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
}
</style>
