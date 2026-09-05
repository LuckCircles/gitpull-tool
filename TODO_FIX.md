一键更新功能，就是把所有的需要更新的代码都更新

增加一个更新所选按钮，就是更新选择需要更新的仓库

如果仓库变动比较大，检查一下是否删库，是否文件清零

在版本历史界面使用[Pivot](https://pyqt-fluent-widgets.readthedocs.io/zh-cn/latest/autoapi/qfluentwidgets/components/navigation/pivot/index.html#qfluentwidgets.components.navigation.pivot.Pivot制作一个远端历史和本地历史导航，远端历史窗口不再使用dialog，改用qwidget。

整体主界面使用FramelessWindow和[NavigationInterface](https://pyqt-fluent-widgets.readthedocs.io/zh-cn/latest/autoapi/qfluentwidgets/components/navigation/navigation_interface/index.html#qfluentwidgets.components.navigation.navigation_interface.NavigationInterface)做窗口和导航，代码参考https://github.com/zhiyiYo/PyQt-Fluent-Widgets/blob/master/examples/navigation/navigation2/demo.py
修复

![image-20260905184754066](C:\Users\luck\AppData\Roaming\Typora\typora-user-images\image-20260905184754066.png)错误，，本地仓库从未做过修改，一直都是pull状态。

屏蔽fluent推广信息

with contextlib.redirect_stdout(io.StringIO()):
    import qfluentwidgets.common.config 



优化算法逻辑，避免产生lock file，产生pull问题。