<template>
  <div class="zoom-controls">
    <button class="zc-btn" title="放大" @click="$emit('zoomIn')">＋</button>
    <button class="zc-btn" title="缩小" @click="$emit('zoomOut')">－</button>
    <button class="zc-btn" title="适应屏幕" @click="$emit('fit')">⤢</button>
    <button class="zc-btn" title="重置布局" @click="$emit('reset')">⟳</button>
    <div class="zc-divider"></div>
    <button
      class="zc-btn"
      :class="{ active: panMode }"
      :title="panMode ? '拖动平移：开（按住拖动图谱）' : '拖动平移：关'"
      @click="$emit('togglePan')"
    >✋</button>
    <div class="zc-zoom-label">{{ zoomPercent }}%</div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  zoomPercent: number
  panMode: boolean
}>()

defineEmits<{
  (e: 'zoomIn'): void
  (e: 'zoomOut'): void
  (e: 'fit'): void
  (e: 'reset'): void
  (e: 'togglePan'): void
}>()
</script>

<style scoped>
.zoom-controls {
  position: absolute;
  right: 14px;
  bottom: 14px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  background: rgba(14, 26, 48, 0.94);
  border: 1px solid #243556;
  border-radius: 10px;
  padding: 6px;
  z-index: 100;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  backdrop-filter: blur(8px);
}
.zc-btn {
  width: 30px;
  height: 30px;
  border: 1px solid #243556;
  background: #13203A;
  color: #C0CCE6;
  border-radius: 6px;
  cursor: pointer;
  font-size: 15px;
  line-height: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.zc-btn:hover { background: #243556; color: #E6EDF7; }
.zc-btn.active { background: #F5A524; border-color: #F5A524; color: #0B1426; }
.zc-divider { width: 22px; height: 1px; background: #243556; margin: 2px 0; }
.zc-zoom-label { font-size: 10px; color: #7A8BAA; margin-top: 2px; font-family: 'JetBrains Mono', monospace; }
</style>
