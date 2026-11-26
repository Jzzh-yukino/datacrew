<script setup lang="ts">
import type { MetricDraft } from '@/types/events'
import BaseButton from './base/BaseButton.vue'

defineProps<{ definition: MetricDraft['definition']; disabled: boolean }>()
const emit = defineEmits<{ (e: 'decide', approved: boolean): void }>()
</script>

<template>
  <section class="card metric-confirmation rise" aria-labelledby="metric-draft-name">
    <h2 id="metric-draft-name" class="card-title">{{ definition.name }}</h2>
    <dl>
      <dt>统计对象</dt><dd>{{ definition.population }}</dd>
      <dt>计算公式</dt><dd>{{ definition.formula }}</dd>
      <dt>筛选条件</dt><dd>{{ definition.filters.join('、') || '无' }}</dd>
      <dt>时间口径</dt><dd>{{ definition.time_basis }}</dd>
      <dt>单位</dt><dd>{{ definition.unit }}</dd>
    </dl>
    <p>确认后在当前会话沿用该口径。</p>
    <div class="actions">
      <BaseButton variant="ok" :disabled="disabled" @click="emit('decide', true)">确认并查询</BaseButton>
      <BaseButton variant="ghost" :disabled="disabled" @click="emit('decide', false)">拒绝</BaseButton>
    </div>
  </section>
</template>

<style scoped>
.metric-confirmation { border-left: 3px solid var(--warn); }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 8px 14px; margin: 12px 0; }
dt, p { color: var(--text-dim); font-size: 13px; }
dd { margin: 0; overflow-wrap: anywhere; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
</style>
