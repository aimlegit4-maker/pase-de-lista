package com.aimlegit.nNotchs;

import org.bukkit.Material;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityRegainHealthEvent;
import org.bukkit.event.player.PlayerItemConsumeEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.potion.PotionEffect;
import org.bukkit.potion.PotionEffectType;
import org.bukkit.plugin.java.JavaPlugin;

public final class NNotchs extends JavaPlugin implements Listener {
    private static final long DEFAULT_REGENERATION_INTERVAL_TICKS = 10L;
    private static final double DEFAULT_EXHAUSTION_PER_HEART = 6.0;
    private static final double EXHAUSTION_PER_SATURATION = 4.0;

    private long regenerationIntervalTicks;
    private double exhaustionPerHeart;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        loadRegenerationSettings();
        getServer().getPluginManager().registerEvents(this, this);
        getServer().getScheduler().runTaskTimer(this, this::regenerateFromSaturation, 1L,
                regenerationIntervalTicks);
    }

    private void loadRegenerationSettings() {
        regenerationIntervalTicks = getConfig().getLong("saturation.regeneration-interval-ticks",
                DEFAULT_REGENERATION_INTERVAL_TICKS);
        if (regenerationIntervalTicks < 1L) {
            getLogger().warning("saturation.regeneration-interval-ticks must be at least 1; using "
                    + DEFAULT_REGENERATION_INTERVAL_TICKS + ".");
            regenerationIntervalTicks = DEFAULT_REGENERATION_INTERVAL_TICKS;
        }

        exhaustionPerHeart = getConfig().getDouble("saturation.exhaustion-per-heart",
                DEFAULT_EXHAUSTION_PER_HEART);
        if (!Double.isFinite(exhaustionPerHeart) || exhaustionPerHeart <= 0.0) {
            getLogger().warning("saturation.exhaustion-per-heart must be a finite number greater than 0; using "
                    + DEFAULT_EXHAUSTION_PER_HEART + ".");
            exhaustionPerHeart = DEFAULT_EXHAUSTION_PER_HEART;
        }
    }

    private void regenerateFromSaturation() {
        for (Player player : getServer().getOnlinePlayers()) {
            if (player.getFoodLevel() < 18 || player.getSaturation() <= 0.0f
                    || player.getHealth() >= player.getMaxHealth()) {
                continue;
            }

            double saturationCost = exhaustionPerHeart / EXHAUSTION_PER_SATURATION;
            float saturation = player.getSaturation();
            if (saturation < saturationCost) {
                player.setSaturation(0.0f);
                continue;
            }

            double healthBefore = player.getHealth();
            player.heal(2.0);
            if (player.getHealth() > healthBefore) {
                player.setSaturation((float) (saturation - saturationCost));
            }
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onNaturalRegain(EntityRegainHealthEvent event) {
        if (event.getEntity() instanceof Player
                && event.getRegainReason() == EntityRegainHealthEvent.RegainReason.SATIATED) {
            event.setCancelled(true);
        }
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onNotchConsume(PlayerItemConsumeEvent event) {
        ItemStack consumedItem = event.getItem();
        Material itemType = consumedItem.getType();
        boolean enchantedGoldenApple = itemType == Material.ENCHANTED_GOLDEN_APPLE;
        if (!enchantedGoldenApple && itemType != Material.GOLDEN_APPLE) {
            return;
        }

        Player player = event.getPlayer();
        if (enchantedGoldenApple) {
            ItemStack remainder = consumedItem.clone();
            remainder.setAmount(remainder.getAmount() - 1);
            event.setItem(new ItemStack(Material.GOLDEN_APPLE));
            event.setReplacement(remainder.getAmount() > 0 ? remainder : new ItemStack(Material.AIR));
        }

        getServer().getScheduler().runTask(this, () -> {
            if (player.isOnline()) {
                if (enchantedGoldenApple) {
                    player.addPotionEffect(new PotionEffect(PotionEffectType.REGENERATION, 20 * 20, 1), true);
                    player.addPotionEffect(new PotionEffect(PotionEffectType.ABSORPTION, 120 * 20, 3), true);
                }
            }
        });
    }

    @Override
    public void onDisable() {
        // Plugin shutdown logic
    }
}
