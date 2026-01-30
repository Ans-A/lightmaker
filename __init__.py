bl_info = {
    "name": "Batch Import/Export",
    "author": "Al Ansari",
    "version": (0, 4, 3),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Lightmap Baker",
    "description": "Non-destructive Lightmap Baking with NumPy optimization",
    "category": "Bake",
}

import numpy as np
import bpy
import os
from bpy.props import StringProperty, IntProperty
from bpy.types import PropertyGroup, Operator, Panel
import pathlib


class BakeState:
    def __init__(self, context, obj):
        self.context = context
        self.obj = obj
        self.original_engine = context.scene.render.engine
        self.original_device = context.scene.cycles.device
        self.original_active = context.view_layer.objects.active
        self.original_selected = context.selected_objects
        self.created_nodes = []

    def __enter__(self):
        self.context.scene.render.engine = 'CYCLES'
        cycles_prefs = self.context.preferences.addons.get("cycles")
        if cycles_prefs and cycles_prefs.preferences.has_active_device():
            self.context.scene.cycles.device = 'GPU'
        
        if self.context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        for mat, node_name in self.created_nodes:
            if mat and mat.use_nodes:
                node = mat.node_tree.nodes.get(node_name)
                if node:
                    mat.node_tree.nodes.remove(node)
        
        self.context.scene.render.engine = self.original_engine
        self.context.scene.cycles.device = self.original_device
        
        bpy.ops.object.select_all(action='DESELECT')
        for o in self.original_selected:
            try: o.select_set(True)
            except: pass
            
        if self.original_active:
            self.context.view_layer.objects.active = self.original_active

    def track_node(self, material, node_name):
        self.created_nodes.append((material, node_name))



class TEX_OT_BakeItem(Operator):
    bl_idname = "my_list.bake_item"
    bl_label = "Bake Lightmap"
    bl_description = "Bakes AO/Shadow, mixes via NumPy, and cleans up."

    def ensure_uv_map(self, obj, uv_map_name):
        if uv_map_name not in obj.data.uv_layers:
            obj.data.uv_layers.new(name=uv_map_name)
        obj.data.uv_layers[uv_map_name].active = True

    def ensure_valid_material(self, obj):

        if not obj.data.materials:
            mat = bpy.data.materials.new(name=f"{obj.name}_Bake_Mat")
            mat.use_nodes = True
            obj.data.materials.append(mat)
            return True


        has_valid = False
        for i, mat in enumerate(obj.data.materials):
            if mat is None:
                mat = bpy.data.materials.new(name=f"{obj.name}_Bake_Mat")
                mat.use_nodes = True
                obj.data.materials[i] = mat
                has_valid = True
            else:
                has_valid = True
        
        return has_valid

    def create_image(self, geo_name, suffix, save_path):
        image_name = geo_name + suffix
        if not os.path.exists(save_path):
            os.makedirs(save_path)
        
        full_path = os.path.join(save_path, image_name)
        
        if image_name in bpy.data.images:
            img = bpy.data.images[image_name]
            img.filepath = full_path
        else:
            img = bpy.data.images.new(image_name, 1024, 1024)
            img.filepath = full_path
            img.save()
        return img

    def setup_bake_nodes(self, obj, image, node_name, state_manager):
        valid_setup = False
        
        for i, slot in enumerate(obj.material_slots):
            mat = slot.material
            if not mat: continue
                
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            
            for n in nodes: n.select = False
            
            if node_name in nodes:
                node = nodes[node_name]
            else:
                node = nodes.new('ShaderNodeTexImage')
                node.name = node_name
                state_manager.track_node(mat, node_name)
            
            node.image = image
            node.select = True
            nodes.active = node 
            valid_setup = True
            
        return valid_setup

    def bake_process(self, context, obj):
        if obj.type != 'MESH': return

        if not self.ensure_valid_material(obj):
            self.report({'ERROR'}, f"Could not create material for {obj.name}")
            return

        with BakeState(context, obj) as state:
            self.ensure_uv_map(obj, 'lightmap')
            
            raw_path = context.scene.TEX_bakes_path
            save_path = os.path.abspath(bpy.path.abspath(raw_path))
            
            shadow_img = self.create_image(obj.name, '_shadowTEX.jpg', save_path)
            ao_img = self.create_image(obj.name, '_aoTEX.jpg', save_path)
            final_img = self.create_image(obj.name, '_combinedTEX.jpg', save_path) 

            context.scene.render.bake.use_selected_to_active = False


            if self.setup_bake_nodes(obj, shadow_img, 'Bake_Node_Temp', state):
                bpy.ops.object.bake(type='SHADOW', save_mode='INTERNAL')
                shadow_img.save()
            else:
                self.report({'ERROR'}, "Failed to setup nodes.")
                return

            if self.setup_bake_nodes(obj, ao_img, 'Bake_Node_Temp', state):
                bpy.ops.object.bake(type='AO', save_mode='INTERNAL')
                ao_img.save()


            try:
                count = len(ao_img.pixels)
                ao_arr = np.empty(count, dtype=np.float32)
                sh_arr = np.empty(count, dtype=np.float32)
                
                ao_img.pixels.foreach_get(ao_arr)
                shadow_img.pixels.foreach_get(sh_arr)
                
                combined = (ao_arr + sh_arr) * 0.5
                
                final_img.pixels.foreach_set(combined)
                final_img.save()
                
            except Exception as e:
                self.report({'ERROR'}, f"NumPy Error: {e}")
                return


            if obj.active_material:
                self.link_result(obj.active_material, final_img)

    def get_node(self, tree, name, type_name):
        if name in tree.nodes: return tree.nodes[name]
        node = tree.nodes.new(type_name)
        node.name = name
        return node

    def link_result(self, mat, image):
        if not mat.use_nodes: return
        tree = mat.node_tree
        
        if "glTF Material Output" not in bpy.data.node_groups:
            bpy.ops.my_list.my_group()
        
        group_node = self.get_node(tree, "glTF Material Output", "ShaderNodeGroup")
        group_node.node_tree = bpy.data.node_groups["glTF Material Output"]
        group_node.location = (300, 300)
        
        tex_node = self.get_node(tree, "Lightmap_Tex", "ShaderNodeTexImage")
        tex_node.image = image
        tex_node.location = (-300, 300)
        

        sep_node = self.get_node(tree, "Lightmap_Sep", "ShaderNodeSeparateColor")
        sep_node.location = (0, 300)
        
        uv_node = self.get_node(tree, "Lightmap_UV", "ShaderNodeUVMap")
        uv_node.uv_map = "lightmap"
        uv_node.location = (-500, 300)
        
        links = tree.links
        links.new(uv_node.outputs[0], tex_node.inputs[0])
        links.new(tex_node.outputs[0], sep_node.inputs[0])

        links.new(sep_node.outputs[0], group_node.inputs[0]) 

    def execute(self, context):
        objs = [o for o in context.selected_objects if o.type == 'MESH']
        
        if not objs:
            self.report({'WARNING'}, "Select a mesh first")
            return {'CANCELLED'}

        for obj in objs:
            bpy.ops.object.select_all(action='DESELECT')
            obj.select_set(True)
            context.view_layer.objects.active = obj
            
            try:
                self.bake_process(context, obj)
            except Exception as e:
                self.report({'ERROR'}, f"Error on {obj.name}: {e}")
                import traceback
                traceback.print_exc()
        
        for obj in objs:
            obj.select_set(True)
            
        return {'FINISHED'}


class BaseList(PropertyGroup):
    name: StringProperty(name="Name", default="") 
    random_prop: StringProperty(name="Info", default="") 

class List_of_glTFs(BaseList):
    pass

class LIST_OT_DeleteItem(Operator):
    bl_idname = "my_list.delete_item"
    bl_label = "Deletes an item"
    bl_description = "Delete an item from the list"
    @classmethod
    def poll(cls, context):
        return context.scene.my_glTFs

    def execute(self, context):
        my_glTFs = context.scene.my_glTFs
        index = context.scene.list_index
        my_glTFs.remove(index)
        context.scene.list_index = min(max(0, index - 1), len(my_glTFs) - 1)
        return{'FINISHED'}

class LIST_OT_CreateGroup(Operator):
    bl_idname = "my_list.my_group"
    bl_label = "create group"
    bl_description = "Create glTF necessary nodetree to be appended before export"

    def execute(self, context):
        group_name = "glTF Material Output"
        if group_name in bpy.data.node_groups:
            return {'FINISHED'}

        group = bpy.data.node_groups.new(type="ShaderNodeTree", name=group_name)
        bpy.data.node_groups[group_name].use_fake_user = True

        group.interface.new_socket(name='Occlusion', in_out='INPUT', socket_type='NodeSocketFloat')
        group.interface.new_socket(name='Thickness', in_out='INPUT', socket_type='NodeSocketFloat')

        group.nodes.new("NodeGroupInput") 
        group.nodes.new("NodeGroupOutput") 
        return{'FINISHED'}

class LIST_OT_ExportglTF(Operator):
    bl_idname = "my_list.export_item"
    bl_label =  "Export glTF"
    
    def execute(self, context):
        obj = context.view_layer.objects.active
        if not obj: return {'CANCELLED'}
        
        raw_path = context.scene.glTF_export_path
        path = pathlib.Path(bpy.path.abspath(raw_path))
        path.mkdir(parents=True, exist_ok=True)
        
        export_path = str(path / (obj.name + ".gltf"))
        bpy.ops.export_scene.gltf(filepath=export_path, use_selection=True)
        
        item = context.scene.my_glTFs.add()
        item.name = export_path
        return {'FINISHED'}

class ADDON_PT_my_panel(Panel):
    bl_label = "Bake Lightmap"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Lightmap Baker"

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        
        col = layout.column(align=True) 
        col.prop(scene, 'TEX_bakes_path')
        col.separator()
        
        row = col.row(align=True)
        row.scale_y = 1.5
        row.operator('my_list.bake_item', text='Bake & Mix', icon='RENDER_STILL')
        
        col.separator()
        col.prop(scene, 'glTF_export_path')
        col.operator('my_list.export_item', text='Export glTF', icon='EXPORT')
        
        col.separator()
        col.label(text="History:")
        col.template_list("UI_UL_list", "The_List", scene, "my_glTFs", scene, "list_index", rows=3)
        col.operator('my_list.delete_item', text='Clear Entry', icon='TRASH')

def register():
    bpy.utils.register_class(LIST_OT_DeleteItem)
    bpy.utils.register_class(LIST_OT_CreateGroup)
    bpy.utils.register_class(TEX_OT_BakeItem)
    bpy.utils.register_class(LIST_OT_ExportglTF)
    bpy.utils.register_class(ADDON_PT_my_panel)
    bpy.utils.register_class(BaseList)
    bpy.utils.register_class(List_of_glTFs)

    bpy.types.Scene.TEX_bakes_path = bpy.props.StringProperty(
        name='Bakes Path', subtype='DIR_PATH', default="//Bakes/")
    
    bpy.types.Scene.glTF_export_path = bpy.props.StringProperty(
        name='Export Path', subtype='DIR_PATH', default="//Exports/")

    bpy.types.Scene.my_glTFs = bpy.props.CollectionProperty(type=List_of_glTFs)
    bpy.types.Scene.list_index = IntProperty(default=0)

def unregister():
    del bpy.types.Scene.TEX_bakes_path
    del bpy.types.Scene.glTF_export_path
    del bpy.types.Scene.my_glTFs
    del bpy.types.Scene.list_index
    
    bpy.utils.unregister_class(LIST_OT_DeleteItem)
    bpy.utils.unregister_class(LIST_OT_CreateGroup)
    bpy.utils.unregister_class(TEX_OT_BakeItem)
    bpy.utils.unregister_class(LIST_OT_ExportglTF)
    bpy.utils.unregister_class(ADDON_PT_my_panel)
    bpy.utils.unregister_class(BaseList)
    bpy.utils.unregister_class(List_of_glTFs)

if __name__ == "__main__":
    register()