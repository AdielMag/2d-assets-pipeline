// Installed by the 2D Assets Pipeline tool. Reads Assets/Sprites/Atlases/atlases.json (the
// exported domain tree) and creates/rebuilds one SpriteAtlas per domain, packing that
// domain's own folder only — ancestor domains (e.g. Common) stay in their own atlas
// and are referenced across atlases by screens, never duplicated into a child domain's
// atlas. Requires Unity 2021.3+ (SpriteAtlasAsset scripting API).
//
// The pipeline's export step already writes a best-effort .spriteatlas file directly
// (no Unity step needed at all in the common case), but hand-authoring Unity's native
// asset format outside the Editor is inherently fragile across versions. This script
// runs automatically (InitializeOnLoadMethod, not just the menu item below) every time
// the project opens or scripts recompile, rebuilding every domain's atlas via Unity's
// own SpriteAtlasAsset API — so even if the pipeline's hand-written file was wrong for
// this Unity version, it self-heals within moments of opening the project, still with
// zero manual clicks.
using System;
using System.IO;
using UnityEditor;
using UnityEditor.U2D;
using UnityEngine;
using UnityEngine.U2D;

public static class SpriteAtlasBuilder
{
    private const string ManifestPath = "Assets/Sprites/Atlases/atlases.json";

    [InitializeOnLoadMethod]
    private static void AutoBuildOnLoad()
    {
        // Defer past the initial load: AssetDatabase writes during InitializeOnLoad
        // itself can race the editor's own import pass.
        EditorApplication.delayCall += () =>
        {
            if (File.Exists(ManifestPath))
                BuildAll();
        };
    }

    [Serializable]
    private class AtlasNode
    {
        public string name;
        public string parent;
        public string folder;
    }

    [Serializable]
    private class AtlasDoc
    {
        public AtlasNode[] atlases;
    }

    [MenuItem("Tools/AssetPipeline/Build Sprite Atlases")]
    public static void BuildAll()
    {
        if (!File.Exists(ManifestPath))
        {
            Debug.LogWarning($"SpriteAtlasBuilder: {ManifestPath} not found — export a project from the pipeline first.");
            return;
        }

        AtlasDoc doc;
        try
        {
            doc = JsonUtility.FromJson<AtlasDoc>(File.ReadAllText(ManifestPath));
        }
        catch (Exception e)
        {
            Debug.LogError($"SpriteAtlasBuilder: could not parse {ManifestPath}: {e.Message}");
            return;
        }

        if (doc?.atlases == null || doc.atlases.Length == 0)
        {
            Debug.LogWarning("SpriteAtlasBuilder: no domains in atlases.json.");
            return;
        }

        int built = 0;
        foreach (var node in doc.atlases)
        {
            if (BuildOne(node))
                built++;
        }

        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();
        Debug.Log($"SpriteAtlasBuilder: built {built}/{doc.atlases.Length} sprite atlas(es).");
    }

    private static bool BuildOne(AtlasNode node)
    {
        var folderAsset = AssetDatabase.LoadAssetAtPath<UnityEngine.Object>(node.folder);
        if (folderAsset == null)
        {
            Debug.LogWarning($"SpriteAtlasBuilder: folder not found for domain '{node.name}': {node.folder} (skipped — no sprites exported into it yet).");
            return false;
        }

        string atlasPath = $"Assets/Sprites/Atlases/{node.name}.spriteatlas";

        // Always rebuild fresh rather than mutate an existing asset's packable list, so
        // re-exports never accumulate stale/duplicate entries.
        var atlas = new SpriteAtlasAsset();

        var packing = new SpriteAtlasPackingSettings
        {
            enableRotation = false,
            enableTightPacking = false,
            padding = 4,
        };
        atlas.SetPackingSettings(packing);

        var textureSettings = new SpriteAtlasTextureSettings
        {
            filterMode = FilterMode.Bilinear,
            generateMipMaps = false,
            readable = false,
        };
        atlas.SetTextureSettings(textureSettings);

        var platform = atlas.GetPlatformSettings("DefaultTexturePlatform");
        platform.maxTextureSize = 2048;
        platform.format = TextureImporterFormat.Automatic;
        atlas.SetPlatformSettings(platform);

        atlas.Add(new[] { folderAsset });

        SpriteAtlasAsset.Save(atlas, atlasPath);
        return true;
    }
}
