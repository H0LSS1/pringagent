import { createApp } from 'vue'
import { createPinia } from 'pinia'
// Element Plus：Vue3 最常用的 UI 组件库（按钮/表格/表单/布局等开箱即用）
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(createPinia())
app.use(router)
// locale=zhCn：组件内文案（分页、日期选择等）显示为中文
app.use(ElementPlus, { locale: zhCn })

app.mount('#app')
