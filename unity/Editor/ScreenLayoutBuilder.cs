// Installed by the 2D Assets Pipeline tool. Reconstructs a UI screen from a
// "<Screen>.screen.json" layout exported by the pipeline: builds a uGUI Canvas sized to
// the layout's reference resolution, places one Image per sprite element (anchored to
// its normalized rect, fit per its "fit" mode) and one text object per text label (TMP if
// available, legacy Text otherwise — see below), then saves the whole hierarchy as
// "Assets/Screens/<Screen>.prefab".
//
// Rebuilds automatically two ways, so no manual step is ever required regardless of
// whether Unity was already open when the pipeline exported a screen:
//   - InitializeOnLoadMethod (like SpriteAtlasBuilder.cs) rebuilds every
//     Assets/Screens/*.screen.json on project open / script recompile.
//   - An AssetPostprocessor below watches for *.screen.json files being written or
//     changed while the Editor is already running, and rebuilds just those — the
//     pipeline writes this file straight to disk from outside Unity, so nothing else
//     would ever notice a re-export happened until the next domain reload otherwise.
// The menu items below let you force a rebuild by hand too.
//
// Usage: Tools > ClashUp > Build Screen from JSON...  (pick a *.screen.json)
//        Tools > ClashUp > Build All Screens          (every Assets/Screens/*.screen.json)
//
// Label elements use TextMeshPro when the target project has the package installed,
// found via reflection (Type.GetType below) rather than a hard `using TMPro;` reference —
// this script must always compile even in a project that has never added TextMeshPro, or
// a single unresolved type here fails the WHOLE editor assembly's compile, silently
// disabling every script in it (this pipeline's importer/atlas builder included). Falls
// back to legacy UnityEngine.UI.Text, which needs no package at all.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using UnityEngine.UI;

public static class ScreenLayoutBuilder
{
    [Serializable]
    private class Rect01 { public float x, y, w, h; }

    [Serializable]
    private class Element
    {
        public string kind = "sprite";  // "sprite" | "label"
        public string asset;
        public string type;
        public string sprite;   // e.g. "Assets/Sprites/UI/PlayButton.png" (sprite elements only)
        public Rect01 rect;     // normalized 0..1, relative to whichever parent this nests under
        public int z;
        public string fit = "stretch";  // "stretch" | "contain" | "slice" (sprite elements only)
        public bool mirror;
        // Index into the elements array of the element this one nests under, or -1 for
        // top-level (directly under the screen's Canvas). Always refers to an earlier
        // index, since the exporter places every element after whatever it nests under.
        public int parent = -1;
        public string text;     // label elements only
        public string color = "#FFFFFF";  // label elements only
        public string align = "center";   // label elements only
    }

    [Serializable]
    private class ScreenDoc
    {
        public string screen;
        public string reference = "1080x1920";
        public Element[] elements;
    }

    private const string ScreensDir = "Assets/Screens";

    [InitializeOnLoadMethod]
    private static void AutoBuildOnLoad()
    {
        // Defer past the initial load: AssetDatabase writes during InitializeOnLoad
        // itself can race the editor's own import pass.
        EditorApplication.delayCall += () =>
        {
            if (Directory.Exists(ScreensDir))
                BuildAll();
        };
    }

    // Catches a screen being (re-)exported while the Editor is already open and running —
    // Unity notices the changed/new *.screen.json file (via its normal file watch / Auto
    // Refresh) and calls this for every import pass, so a screen exported mid-session
    // gets its prefab built without reopening the project or clicking a menu item.
    private class Watcher : AssetPostprocessor
    {
        private static void OnPostprocessAllAssets(
            string[] importedAssets, string[] deletedAssets,
            string[] movedAssets, string[] movedFromAssetPaths)
        {
            List<string> changed = null;
            foreach (string path in importedAssets)
            {
                if (path.EndsWith(".screen.json", StringComparison.OrdinalIgnoreCase))
                    (changed ??= new List<string>()).Add(path);
            }
            if (changed == null)
                return;

            // Defer: building a prefab from inside an active import pass (creating and
            // saving a new asset) is unsafe for the same reason AutoBuildOnLoad above
            // defers past InitializeOnLoad — get out of the postprocessing callback first.
            EditorApplication.delayCall += () =>
            {
                foreach (string path in changed)
                    BuildScreen(path);
                AssetDatabase.SaveAssets();
            };
        }
    }

    [MenuItem("Tools/ClashUp/Build Screen from JSON...")]
    private static void BuildFromPicker()
    {
        string start = Path.Combine(Application.dataPath, "Screens");
        if (!Directory.Exists(start)) start = Application.dataPath;
        string file = EditorUtility.OpenFilePanel("Pick a .screen.json", start, "json");
        if (string.IsNullOrEmpty(file))
            return;
        BuildScreen(file);
    }

    [MenuItem("Tools/ClashUp/Build All Screens")]
    private static void BuildAll()
    {
        if (!Directory.Exists(ScreensDir))
        {
            Debug.LogWarning($"ScreenLayoutBuilder: no folder {ScreensDir}");
            return;
        }
        int built = 0;
        foreach (string file in Directory.GetFiles(ScreensDir, "*.screen.json"))
            if (BuildScreen(file))
                built++;
        AssetDatabase.SaveAssets();
        AssetDatabase.Refresh();
        Debug.Log($"ScreenLayoutBuilder: built {built} screen prefab(s).");
    }

    private static bool BuildScreen(string jsonPath)
    {
        ScreenDoc doc;
        try
        {
            doc = JsonUtility.FromJson<ScreenDoc>(File.ReadAllText(jsonPath));
        }
        catch (Exception e)
        {
            Debug.LogError($"ScreenLayoutBuilder: could not parse {jsonPath}: {e.Message}");
            return false;
        }
        if (doc == null || doc.elements == null)
        {
            Debug.LogError($"ScreenLayoutBuilder: no elements in {jsonPath}");
            return false;
        }

        Vector2 refRes = ParseReference(doc.reference);

        // Build in a Unity "preview scene" — an off-to-the-side scene the main scene
        // stack doesn't know about, meant for exactly this (build a hierarchy, save it,
        // discard the scene). EditorSceneManager.NewScene(Additive) looked equivalent but
        // throws InvalidOperationException the moment the currently open scene is a
        // never-saved "Untitled" scene (e.g. right after opening a fresh project) — a
        // preview scene has no such dependency on the main scene's save state.
        Scene buildScene = EditorSceneManager.NewPreviewScene();
        GameObject root;
        try
        {
            root = new GameObject(
                $"Screen_{doc.screen}", typeof(Canvas), typeof(CanvasScaler), typeof(GraphicRaycaster));
            SceneManager.MoveGameObjectToScene(root, buildScene);
            var canvas = root.GetComponent<Canvas>();
            canvas.renderMode = RenderMode.ScreenSpaceOverlay;
            var scaler = root.GetComponent<CanvasScaler>();
            scaler.uiScaleMode = CanvasScaler.ScaleMode.ScaleWithScreenSize;
            scaler.referenceResolution = refRes;
            scaler.matchWidthOrHeight = 0.5f;

            // Each element nests under an EARLIER element's transform (its "parent" index)
            // or under the screen root — built() fills this array in order so a later
            // element can always look up its own parent's already-built transform.
            var built = new Transform[doc.elements.Length];
            for (int i = 0; i < doc.elements.Length; i++)
            {
                Element el = doc.elements[i];
                Transform parentT = (el.parent >= 0 && el.parent < i) ? built[el.parent] : root.transform;
                GameObject go = el.kind == "label" ? BuildLabel(parentT, el) : BuildSprite(parentT, el);
                built[i] = go.transform;
            }

            Directory.CreateDirectory(ScreensDir);
            string prefabPath = $"{ScreensDir}/{doc.screen}.prefab";
            bool success;
            PrefabUtility.SaveAsPrefabAsset(root, prefabPath, out success);
            if (success)
                Debug.Log($"ScreenLayoutBuilder: built '{prefabPath}' with {doc.elements.Length} elements.");
            else
                Debug.LogError($"ScreenLayoutBuilder: failed to save prefab for '{doc.screen}'.");
            return success;
        }
        finally
        {
            EditorSceneManager.ClosePreviewScene(buildScene);
        }
    }

    private static GameObject BuildSprite(Transform parent, Element el)
    {
        var go = new GameObject(el.asset, typeof(RectTransform), typeof(Image));
        go.transform.SetParent(parent, false);
        ApplyRect(go.GetComponent<RectTransform>(), el.rect, el.mirror);

        var img = go.GetComponent<Image>();
        var sprite = AssetDatabase.LoadAssetAtPath<Sprite>(el.sprite);
        if (sprite == null)
            Debug.LogWarning($"ScreenLayoutBuilder: sprite not found: {el.sprite}");
        img.sprite = sprite;
        img.raycastTarget = false;

        switch (el.fit)
        {
            case "slice":
                // Border comes from the Sprite's own imported border (baked on by
                // AssetPipelineImporter.cs) — no border math needed here.
                img.type = Image.Type.Sliced;
                break;
            case "contain":
                img.type = Image.Type.Simple;
                img.preserveAspect = true;
                break;
            default:  // "stretch"
                img.type = Image.Type.Simple;
                img.preserveAspect = false;
                break;
        }
        return go;
    }

    // Resolved once, lazily: null when the target project has no TextMeshPro package, in
    // which case BuildLabel falls back to legacy Text below.
    private static readonly Type TmpTextType =
        Type.GetType("TMPro.TextMeshProUGUI, Unity.TextMeshPro");

    private static GameObject BuildLabel(Transform parent, Element el)
    {
        var go = new GameObject(el.asset, typeof(RectTransform));
        go.transform.SetParent(parent, false);
        ApplyRect(go.GetComponent<RectTransform>(), el.rect, el.mirror);

        if (TmpTextType != null)
            BuildTmpLabel(go, el);
        else
            BuildLegacyLabel(go, el);
        return go;
    }

    private static void BuildTmpLabel(GameObject go, Element el)
    {
        var label = go.AddComponent(TmpTextType);
        var t = TmpTextType;
        t.GetProperty("text").SetValue(label, el.text ?? "");
        t.GetProperty("raycastTarget").SetValue(label, false);
        t.GetProperty("color").SetValue(label, ParseColor(el.color));
        var alignmentProp = t.GetProperty("alignment");
        string alignmentName = el.align switch
        {
            "left" => "Left",
            "right" => "Right",
            _ => "Center",
        };
        alignmentProp.SetValue(label, Enum.Parse(alignmentProp.PropertyType, alignmentName));
        // No font assigned: falls back to TMP_Settings.defaultFontAsset.
    }

    private static void BuildLegacyLabel(GameObject go, Element el)
    {
        var label = go.AddComponent<Text>();
        label.text = el.text ?? "";
        label.raycastTarget = false;
        label.color = ParseColor(el.color);
        label.alignment = el.align switch
        {
            "left" => TextAnchor.MiddleLeft,
            "right" => TextAnchor.MiddleRight,
            _ => TextAnchor.MiddleCenter,
        };
        label.font = Resources.GetBuiltinResource<Font>("LegacyRuntime.ttf");
    }

    private static void ApplyRect(RectTransform rt, Rect01 rect, bool mirror)
    {
        // JSON rect is top-left origin; uGUI anchors are bottom-left origin, so flip Y.
        float x = rect.x, y = rect.y, w = rect.w, h = rect.h;
        rt.anchorMin = new Vector2(x, 1f - (y + h));
        rt.anchorMax = new Vector2(x + w, 1f - y);
        rt.offsetMin = Vector2.zero;
        rt.offsetMax = Vector2.zero;
        // Anchors already place/size the rect; scaling around the centered pivot mirrors
        // it in place without affecting position or size.
        if (mirror)
            rt.localScale = new Vector3(-1f, 1f, 1f);
    }

    private static Color ParseColor(string hex)
    {
        if (!string.IsNullOrEmpty(hex) && ColorUtility.TryParseHtmlString(hex, out var c))
            return c;
        return Color.white;
    }

    private static Vector2 ParseReference(string reference)
    {
        try
        {
            string[] parts = reference.Split('x');
            return new Vector2(
                float.Parse(parts[0], CultureInfo.InvariantCulture),
                float.Parse(parts[1], CultureInfo.InvariantCulture));
        }
        catch
        {
            return new Vector2(1080f, 1920f);
        }
    }
}
