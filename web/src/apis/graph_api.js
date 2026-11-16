import { apiGet, buildQuery } from './base'

export const graphApi = {
  getGraphs: async () => {
    return await apiGet('/api/graph/list', {}, true)
  },

  getSubgraph: async (params) => {
    const {
      kb_id,
      file_id,
      node_label = '*',
      max_depth = 2,
      max_nodes = 100,
      exclude_chunk = false
    } = params

    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = buildQuery({ kb_id, file_id, node_label, max_depth, max_nodes, exclude_chunk })

    return await apiGet(`/api/graph/subgraph?${queryParams}`, {}, true)
  },

  getStats: async (kb_id, file_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = buildQuery({ kb_id, file_id })
    return await apiGet(`/api/graph/stats?${queryParams}`, {}, true)
  },

  getLabels: async (kb_id, file_id) => {
    if (!kb_id) {
      throw new Error('kb_id is required')
    }

    const queryParams = buildQuery({ kb_id, file_id })
    return await apiGet(`/api/graph/labels?${queryParams}`, {}, true)
  }
}

export const unifiedApi = graphApi
