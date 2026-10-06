<template>
  <div>
    <h1>{{ props.layer }} 层 · 履历先标冲正</h1>
    <span v-for="x in rows" :key="x.id" class="lot">{{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
  </div>
</template>
<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { api } from '../api'
const props = defineProps({ layer: String })
const rows = ref([])
async function load() { rows.value = await api('/fridge?layer=' + props.layer) }
watch(() => props.layer, load)
onMounted(() => {
  load()
  window.addEventListener('pantry:changed', load)
})
onUnmounted(() => window.removeEventListener('pantry:changed', load))
</script>
