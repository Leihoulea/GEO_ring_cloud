<template>
  <div class="filter-panel">
    <div class="panel-section">
      <h4>视图</h4>
      <div class="view-tabs">
        <button :class="{ active: view === 'galaxy' }" @click="$emit('changeView', 'galaxy')">星系</button>
        <button :class="{ active: view === 'pipeline' }" @click="$emit('changeView', 'pipeline')">管道</button>
        <button :class="{ active: view === 'callgraph' }" @click="$emit('changeView', 'callgraph')">调用图</button>
      </div>
    </div>

    <div class="panel-section">
      <h4>搜索</h4>
      <input
        type="text"
        :value="search"
        @input="$emit('update:search', ($event.target as HTMLInputElement).value)"
        placeholder="节点名 / 路径 / docstring..."
        class="search-input"
      />
    </div>

    <div class="panel-section">
      <h4>节点类型 <button class="clear-btn" @click="$emit('clearTypes')">清除</button></h4>
      <div class="checkbox-grid">
        <label v-for="t in allTypes" :key="t">
          <input
            type="checkbox"
            :checked="filters.types.has(t)"
            @change="$emit('toggleType', t)"
          />
          <span class="color-dot" :style="{ background: colors[t] || '#888' }"></span>
          <span>{{ labels[t] || t }}</span>
          <span class="count">{{ typeCounts[t] || 0 }}</span>
        </label>
      </div>
    </div>

    <div class="panel-section" v-if="allStages.length">
      <h4>阶段</h4>
      <div class="checkbox-list">
        <label v-for="s in allStages" :key="s">
          <input
            type="checkbox"
            :checked="filters.stages.has(s)"
            @change="$emit('toggleStage', s)"
          />
          <span>{{ s }}</span>
          <span class="count">{{ stageCounts[s] || 0 }}</span>
        </label>
      </div>
    </div>

    <div class="panel-section">
      <h4>状态</h4>
      <div class="checkbox-list">
        <label v-for="s in ['active', 'deprecated', 'experimental', 'planned']" :key="s">
          <input
            type="checkbox"
            :checked="filters.status.has(s)"
            @change="$emit('toggleStatus', s)"
          />
          <span>{{ s }}</span>
        </label>
      </div>
    </div>

    <div class="panel-section stats">
      <h4>统计</h4>
      <div class="stat-row"><span>节点</span><b>{{ stats.nodes }}</b></div>
      <div class="stat-row"><span>边</span><b>{{ stats.edges }}</b></div>
      <div class="stat-row"><span>脚本</span><b>{{ typeCounts.script || 0 }}</b></div>
      <div class="stat-row"><span>函数/类</span><b>{{ (typeCounts.ast_function||0) + (typeCounts.ast_class||0) + (typeCounts.ast_method||0) }}</b></div>
      <div class="stat-row"><span>报告</span><b>{{ typeCounts.report || 0 }}</b></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { NODE_COLORS, NODE_TYPE_LABELS } from '../types'

defineProps<{
  view: string
  search: string
  filters: { types: Set<string>; stages: Set<string>; status: Set<string>; showCollapsed: boolean }
  allTypes: string[]
  allStages: string[]
  typeCounts: Record<string, number>
  stageCounts: Record<string, number>
  stats: { nodes: number; edges: number }
}>()

defineEmits<{
  (e: 'changeView', v: string): void
  (e: 'update:search', v: string): void
  (e: 'toggleType', t: string): void
  (e: 'toggleStage', s: string): void
  (e: 'toggleStatus', s: string): void
  (e: 'clearTypes'): void
}>()

const colors = NODE_COLORS
const labels = NODE_TYPE_LABELS
</script>

<style scoped>
.filter-panel {
  width: 220px; height: 100%; flex-shrink: 0;
  background: #0E1A30; border-right: 1px solid #243556;
  overflow-y: auto; padding: 14px 12px; box-sizing: border-box;
}
.panel-section { margin-bottom: 18px; }
.panel-section h4 {
  margin: 0 0 8px 0; font-size: 10px; font-weight: 600;
  color: #7A8BAA; text-transform: uppercase; letter-spacing: 0.08em;
  display: flex; justify-content: space-between; align-items: center;
}
.clear-btn {
  background: none; border: 1px solid #243556; color: #7A8BAA;
  font-size: 10px; padding: 0 5px; border-radius: 3px; cursor: pointer;
  text-transform: none; letter-spacing: 0;
}
.clear-btn:hover { color: #F5A524; border-color: #F5A524; }
.view-tabs { display: flex; gap: 4px; }
.view-tabs button {
  flex: 1; padding: 7px 4px; background: #13203A;
  border: 1px solid #243556; color: #7A8BAA;
  border-radius: 5px; cursor: pointer; font-size: 12px; font-weight: 500;
  transition: all 0.15s;
}
.view-tabs button:hover { color: #E6EDF7; }
.view-tabs button.active {
  background: #F5A524; border-color: #F5A524; color: #0B1426; font-weight: 700;
}
.search-input {
  width: 100%; box-sizing: border-box; padding: 7px 10px;
  background: #0B1426; border: 1px solid #243556; color: #E6EDF7;
  border-radius: 5px; font-size: 12px;
}
.search-input:focus { outline: none; border-color: #F5A524; }
.search-input::placeholder { color: #4A5A7A; }
.checkbox-grid, .checkbox-list { display: flex; flex-direction: column; gap: 5px; }
.checkbox-grid label, .checkbox-list label {
  display: flex; align-items: center; gap: 7px; font-size: 11px;
  color: #C0CCE6; cursor: pointer;
}
.checkbox-grid label:hover, .checkbox-list label:hover { color: #E6EDF7; }
.checkbox-grid input, .checkbox-list input { accent-color: #F5A524; }
.color-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.count { margin-left: auto; color: #4A5A7A; font-size: 10px; font-family: 'JetBrains Mono', monospace; }
.stats .stat-row { display: flex; justify-content: space-between; font-size: 11px; padding: 3px 0; color: #7A8BAA; }
.stats .stat-row b { color: #F5A524; font-family: 'JetBrains Mono', monospace; font-weight: 600; }
</style>
