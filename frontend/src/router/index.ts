import { createRouter, createWebHistory } from 'vue-router'

// 路由表：URL -> 页面组件。
// 懒加载（() => import(...)）：页面代码在第一次访问时才下载，首屏更快。
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'monitor', component: () => import('@/views/MonitorView.vue') },
    { path: '/pressure', name: 'pressure', component: () => import('@/views/PressureView.vue') },
    { path: '/chat', name: 'chat', component: () => import('@/views/ChatView.vue') },
  ],
})

export default router
