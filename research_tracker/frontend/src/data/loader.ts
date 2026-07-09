// 图数据加载器：从 src/data/graph.json 加载，并提供派生索引

import type { GraphData, GraphNode, GraphEdge, GraphIndex } from '../types'

export type { GraphIndex }

let cachedGraph: GraphData | null = null

export async function loadGraph(): Promise<GraphData> {
  if (cachedGraph) return cachedGraph
  // 用 fetch 加载运行时数据（避免 Vite 构建时 graph.json 不存在导致报错）
  const candidates = [
    './src/data/graph.json',
    './data/graph.json',
    'graph.json',
  ]
  for (const url of candidates) {
    try {
      const resp = await fetch(url)
      if (resp.ok) {
        cachedGraph = await resp.json() as GraphData
        console.log(`[research_tracker] 已加载图谱: ${url} (${cachedGraph.nodes.length} 节点, ${cachedGraph.edges.length} 边)`)
        return cachedGraph
      }
    } catch {
      // 继续尝试下一个候选路径
    }
  }
  console.error('加载 graph.json 失败，请先运行后端 run_scan.py 生成图谱')
  cachedGraph = { nodes: [], edges: [] }
  return cachedGraph
}

export function buildIndex(graph: GraphData): GraphIndex {
  const nodeById = new Map<string, GraphNode>()
  for (const n of graph.nodes) nodeById.set(n.id, n)

  const outEdges = new Map<string, GraphEdge[]>()
  const inEdges = new Map<string, GraphEdge[]>()
  const childrenByParent = new Map<string, string[]>()
  const parentByChild = new Map<string, string>()

  for (const e of graph.edges) {
    if (!outEdges.has(e.source)) outEdges.set(e.source, [])
    outEdges.get(e.source)!.push(e)
    if (!inEdges.has(e.target)) inEdges.set(e.target, [])
    inEdges.get(e.target)!.push(e)

    if (e.type === 'contains') {
      if (!childrenByParent.has(e.source)) childrenByParent.set(e.source, [])
      childrenByParent.get(e.source)!.push(e.target)
      parentByChild.set(e.target, e.source)
    }
  }

  return { nodeById, outEdges, inEdges, childrenByParent, parentByChild }
}

// 获取节点的所有邻居（含方向）
export function getNeighbors(nodeId: string, index: GraphIndex): Set<string> {
  const neighbors = new Set<string>()
  for (const e of index.outEdges.get(nodeId) || []) neighbors.add(e.target)
  for (const e of index.inEdges.get(nodeId) || []) neighbors.add(e.source)
  return neighbors
}

// 获取节点在某层级的所有后代（递归 contains）
export function getDescendants(nodeId: string, index: GraphIndex, maxDepth = 10): Set<string> {
  const result = new Set<string>()
  const stack = [nodeId]
  let depth = 0
  while (stack.length && depth < maxDepth) {
    const cur = stack.pop()!
    const kids = index.childrenByParent.get(cur) || []
    for (const k of kids) {
      if (!result.has(k)) {
        result.add(k)
        stack.push(k)
      }
    }
    depth++
  }
  return result
}
