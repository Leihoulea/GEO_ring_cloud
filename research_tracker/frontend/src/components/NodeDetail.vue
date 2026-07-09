<template>
  <div class="node-detail">
    <template v-if="node">
      <div class="detail-header">
        <span class="type-badge" :style="{ background: color }">{{ typeLabel }}</span>
        <h3>{{ node.label }}</h3>
      </div>

      <div class="detail-body">
        <div v-if="node.importance === 'critical'" class="imp critical">关键节点</div>
        <div v-else-if="node.importance === 'high'" class="imp high">高重要性</div>

        <div v-if="node.path" class="field">
          <span class="field-label">路径</span>
          <span class="field-value mono">{{ node.path }}</span>
        </div>
        <div v-if="node.qual_name && node.qual_name !== node.label" class="field">
          <span class="field-label">全名</span>
          <span class="field-value mono">{{ node.qual_name }}</span>
        </div>
        <div v-if="node.stage" class="field">
          <span class="field-label">阶段</span>
          <span class="field-value">{{ node.stage }}</span>
        </div>
        <div v-if="node.subproject" class="field">
          <span class="field-label">子项目</span>
          <span class="field-value">{{ node.subproject }}</span>
        </div>
        <div v-if="node.docstring" class="field">
          <span class="field-label">说明</span>
          <span class="field-value">{{ node.docstring }}</span>
        </div>
        <div v-if="node.note" class="field">
          <span class="field-label">标注</span>
          <span class="field-value note">{{ node.note }}</span>
        </div>

        <!-- 函数/类信息 -->
        <div v-if="node.args && node.args.length" class="field">
          <span class="field-label">参数</span>
          <span class="field-value mono">{{ node.args.join(', ') }}</span>
        </div>
        <div v-if="node.returns" class="field">
          <span class="field-label">返回</span>
          <span class="field-value mono">{{ node.returns }}</span>
        </div>
        <div v-if="node.has_main_guard" class="field">
          <span class="field-value">✓ 含 __main__ 入口</span>
        </div>
        <div v-if="node.base_classes && node.base_classes.length" class="field">
          <span class="field-label">基类</span>
          <span class="field-value mono">{{ node.base_classes.join(', ') }}</span>
        </div>
        <div v-if="node.imports && node.imports.length" class="field">
          <span class="field-label">导入</span>
          <span class="field-value mono small">{{ node.imports.slice(0, 15).join(', ') }}{{ node.imports.length > 15 ? '...' : '' }}</span>
        </div>

        <!-- 报告信息 -->
        <div v-if="node.title" class="field">
          <span class="field-label">标题</span>
          <span class="field-value">{{ node.title }}</span>
        </div>
        <div v-if="node.summary" class="field">
          <span class="field-label">摘要</span>
          <span class="field-value small">{{ node.summary }}</span>
        </div>
        <div v-if="node.warnings_count" class="field warn">
          <span class="field-label">警告</span>
          <span class="field-value">{{ node.warnings_count }} 条</span>
        </div>
        <div v-if="node.blocking_count" class="field block">
          <span class="field-label">阻塞</span>
          <span class="field-value">{{ node.blocking_count }} 条</span>
        </div>
        <div v-if="node.results && node.results.length" class="field">
          <span class="field-label">结果</span>
          <div class="results">
            <span v-for="(r, i) in node.results" :key="i" class="result-tag" :class="r.status.toLowerCase()">
              {{ r.status }}
            </span>
          </div>
        </div>

        <!-- CSV 信息 -->
        <div v-if="node.format" class="field">
          <span class="field-label">格式</span>
          <span class="field-value">{{ node.format }} · {{ node.row_count }} 行</span>
        </div>
        <div v-if="node.columns && node.columns.length" class="field">
          <span class="field-label">列</span>
          <span class="field-value mono small">{{ node.columns.join(', ') }}</span>
        </div>
        <div v-if="node.time_range" class="field">
          <span class="field-label">时间范围</span>
          <span class="field-value">{{ node.time_range }}</span>
        </div>
        <div v-if="node.products && node.products.length" class="field">
          <span class="field-label">产品</span>
          <span class="field-value small">{{ node.products.join(', ') }}</span>
        </div>
        <div v-if="node.satellites && node.satellites.length" class="field">
          <span class="field-label">卫星</span>
          <span class="field-value small">{{ node.satellites.join(', ') }}</span>
        </div>

        <div v-if="node.tags && node.tags.length" class="field">
          <span class="field-label">标签</span>
          <div class="tags">
            <span v-for="t in node.tags" :key="t" class="tag">{{ t }}</span>
          </div>
        </div>
      </div>
    </template>
    <div v-else class="empty">
      <p>点击图谱中的节点查看详情</p>
      <p class="sub">双击聚合节点可展开/折叠</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { GraphNode } from '../types'
import { NODE_COLORS, NODE_TYPE_LABELS } from '../types'

const props = defineProps<{ node: GraphNode | null }>()

const color = computed(() => props.node ? (NODE_COLORS[props.node.type] || '#888') : '#888')
const typeLabel = computed(() => props.node ? (NODE_TYPE_LABELS[props.node.type] || props.node.type) : '')
</script>

<style scoped>
.node-detail { padding: 16px; height: 100%; overflow-y: auto; }
.detail-header { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; padding-right: 28px; }
.detail-header h3 { margin: 0; font-size: 14px; word-break: break-all; color: #E6EDF7; font-weight: 700; }
.type-badge {
  padding: 3px 9px; border-radius: 11px; font-size: 11px;
  color: #0B1426; font-weight: 700; white-space: nowrap;
}
.detail-body { font-size: 12px; }
.field { margin-bottom: 10px; display: flex; flex-direction: column; gap: 3px; }
.field-label {
  color: #7A8BAA; font-size: 10px; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.06em;
}
.field-value { color: #C0CCE6; word-break: break-all; line-height: 1.5; }
.field-value.mono { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #5B8DEF; }
.field-value.small { font-size: 11px; }
.field-value.note { color: #F5A524; font-style: italic; }
.field.warn .field-value { color: #E8C76A; }
.field.block .field-value { color: #D67898; }
.imp { padding: 4px 9px; border-radius: 4px; margin-bottom: 10px; font-size: 11px; display: inline-block; font-weight: 600; }
.imp.critical { background: rgba(214,120,152,0.15); color: #D67898; border: 1px solid rgba(214,120,152,0.4); }
.imp.high { background: rgba(245,165,36,0.15); color: #F5A524; border: 1px solid rgba(245,165,36,0.4); }
.results { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.result-tag { padding: 2px 7px; border-radius: 3px; font-size: 10px; font-weight: 600; }
.result-tag.pass, .result-tag.ok { background: rgba(79,179,160,0.18); color: #4FB3A0; }
.result-tag.fail, .result-tag.error { background: rgba(214,120,152,0.18); color: #D67898; }
.result-tag.pass_with_warnings { background: rgba(245,165,36,0.18); color: #F5A524; }
.tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
.tag { background: #1A2A4A; color: #7A8BAA; padding: 2px 7px; border-radius: 3px; font-size: 10px; border: 1px solid #243556; }
.empty { color: #4A5A7A; text-align: center; margin-top: 40px; font-size: 12px; }
.empty .sub { font-size: 11px; margin-top: 6px; }
</style>
