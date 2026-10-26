<script setup lang="ts">
// 应用骨架：左侧菜单 + 顶栏 + 主内容区（router-view 渲染当前路由页面）
import { useRoute } from 'vue-router'

const route = useRoute()
// 菜单高亮跟随当前 URL（el-menu 的 default-active 需要）
const activeMenu = () => route.path
</script>

<template>
  <el-container class="layout">
    <el-aside width="200px" class="aside">
      <div class="logo">印刷工艺智能体</div>
      <el-menu
        :default-active="activeMenu()"
        router
        background-color="#1d2333"
        text-color="#aeb6c8"
        active-text-color="#409eff"
      >
        <el-menu-item index="/">📊 监控大屏</el-menu-item>
        <el-menu-item index="/pressure">🎛️ 压力管理</el-menu-item>
        <el-menu-item index="/chat">💬 工艺智能体</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="header">
        <span>印刷智能化项目</span>
        <span class="sub">FastAPI + Vue3 + MySQL + Milvus + LangGraph</span>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}
.aside {
  background: #1d2333;
}
.logo {
  color: #fff;
  font-weight: 600;
  font-size: 16px;
  padding: 18px 16px;
  border-bottom: 1px solid #2a314a;
}
.aside :deep(.el-menu) {
  border-right: none;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  border-bottom: 1px solid #e4e7ed;
  font-weight: 600;
}
.header .sub {
  font-size: 12px;
  color: #909399;
  font-weight: 400;
}
.main {
  background: #f5f7fa;
}
</style>
